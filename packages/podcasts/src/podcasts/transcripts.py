"""`podcasts-transcribe`: transcripts of the downloaded episodes, and the ads they give away.

Whisper takes about 8 minutes an hour of audio here with every core, so this runs on its
own timer, apart from the sync, at the lowest priority and on as many threads as
PODCASTS_TRANSCRIBE_THREADS gives for the time of day (see Schedule; default 1 of the 4
cores, so the machine stays cool and its fans quiet; 2 got them going). After each episode
it takes the newest one waiting, so a new episode goes ahead of a catalog's backlog. The
original is transcribed, as downloaded (audio.py), so the times stay right whatever is cut
from what is served; the served .vtt is shifted to match (Library.render).

Transcribing holds no lock, and neither does looking for ad reads in the transcript:
AnythingLLM's default model (DeepSeek) reads it and names them (ad_reads.py), or, without a
key or when its answer can't be used, a list of sponsor phrases does (sponsor_text.py).
Each is logged with its opening words. A sync may cut or prune the episode meanwhile, so
the result is applied under the sync lock only if the file is still the one transcribed
(Library.update_episode). Depending on the feed's `ad_words`, the ad reads become the
episode's "ad-read" cuts, left out of what is served ("cut", the default) or only noted as
possible_ads ("report"), or are ignored ("off"); then applying writes
  state/transcripts/<slug>/<key>.json the segments, in the original's times, for search_podcasts
  site/<slug>/<served stem>.vtt       linked from feed.xml as <podcast:transcript>

One run at a time (transcribe.lock); a second exits at once. state/transcribing.json names
the episode Whisper is hearing, and is removed when it stops, even on SIGTERM; a run that
finds it left over marks that episode failed, since the last run died on it (killed, e.g.
out of memory), rather than trying it again.
"""

import os
import signal
import sys
from datetime import datetime, time
from functools import partial
from pathlib import Path

from llm import LLMError

from podcasts.ad_reads import AdReadError, find_ad_reads
from podcasts.files import _read_json, _write_json
from podcasts.library import Library, _scrubbable, clock, key, models_dir
from podcasts.rss import Episode
from podcasts.rules import user_tz
from podcasts.segments import Transcript
from podcasts.sponsor_text import sponsor_spans
from podcasts.whisper import (
    ModelSize,
    TranscribeError,
    TranscriptionRequest,
    WhisperTranscriber,
    load_whisper,
    transcribe,
)

MODEL = ModelSize.BASE


class Schedule:
    """How many threads to transcribe on at each time of day: PODCASTS_TRANSCRIBE_THREADS,
    either a number for all day or `HH:MM=threads` entries, each from that time until the
    next, e.g. `08:00=4,22:00=1` (the last one runs on past midnight). 0 threads pauses
    transcribing until the next entry; a run that reaches one stops, and the timer starts
    the next."""

    def __init__(self, text: str):
        text = text.strip() or "1"
        try:
            if text.isdigit():
                self.entries = [(time(0), int(text))]
            else:
                self.entries = sorted(
                    (time.fromisoformat(at.strip().zfill(5)), int(n))
                    for at, _, n in (part.partition("=") for part in text.split(","))
                )
        except ValueError:
            raise ValueError(
                f"PODCASTS_TRANSCRIBE_THREADS should be a number or like 08:00=4,22:00=1, not {text!r}"
            ) from None
        if any(n < 0 for _, n in self.entries):
            raise ValueError(
                f"PODCASTS_TRANSCRIBE_THREADS can't have negative threads: {text!r}"
            )

    def threads(self, now: time) -> int:
        current = self.entries[-1][
            1
        ]  # before the first entry, the day's last still holds
        for at, n in self.entries:
            if at <= now:
                current = n
        return current


class Scheduled:
    """A transcriber on as many threads as the schedule gives now, loaded again when that
    changes (Whisper takes its thread count when it loads, in a few seconds)."""

    def __init__(
        self, schedule: Schedule, make, clock=lambda: datetime.now(user_tz()).time()
    ):
        self.schedule, self.make, self.clock = schedule, make, clock
        self.loaded, self.inner = 0, None

    def threads(self) -> int:
        return self.schedule.threads(self.clock())

    def transcribe(self, req):
        if (n := self.threads()) != self.loaded:
            self.inner, self.loaded = self.make(n), n
        assert (
            self.inner is not None
        )  # the worker doesn't call while paused (0 threads)
        return self.inner.transcribe(req)


class Worker:
    def __init__(
        self, lib: Library, transcriber, log=print, chat=None, paused=lambda: False
    ):
        """`chat`: the default model, to find ad reads; without it, the phrase list does.
        `paused` is asked before each episode, and stops the run when it says so."""
        self.lib = lib
        self.transcriber = transcriber
        self.store = lib.transcripts
        self.log = log
        self.chat = chat
        self.paused = paused

    def find_ads(self, segments) -> list[tuple[float, float]]:
        if self.chat:
            try:
                return find_ad_reads(self.chat, segments, self.log)
            except (AdReadError, LLMError) as e:
                self.log(
                    f"  the model couldn't look for ads ({e}); using the phrase list"
                )
        return sponsor_spans(segments)

    def todo(self) -> list[tuple[str, Episode, str]]:
        """(slug, episode, its feed's ad_words) of each episode waiting for a transcript,
        newest first."""
        out = []
        for slug, sub in self.lib.feeds().items():
            rec = self.lib.record(slug)
            if not rec:
                continue
            self.store.prune(slug, {key(e) for e in rec["show"].episodes if e.audio})
            if not sub["transcribe"]:
                continue
            for ep in rec["show"].episodes:
                # Without an original, the episode waits for the sync to adopt it.
                if (
                    ep.transcript
                    or ep.transcript_error
                    or not ep.audio
                    or not _scrubbable(ep)
                ):
                    continue
                out.append((slug, ep, sub["ad_words"]))
        return sorted(out, key=lambda x: x[1].published, reverse=True)

    @property
    def marker(self) -> Path:
        """The episode being transcribed, written before and removed after: one still there
        when a run starts is the one the last run died on (killed, e.g. out of memory)."""
        return self.lib.state / "transcribing.json"

    def recover(self) -> None:
        """Mark the episode the last run died on as failed, so it isn't tried forever.
        Only called holding transcribe.lock, so no other run is working on it."""
        try:
            self._mark_died_on()
        finally:
            self.marker.unlink(missing_ok=True)

    def _mark_died_on(self) -> None:
        try:
            if not (last := _read_json(self.marker)):
                return
            slug, guid, audio, started = (
                last[k] for k in ("slug", "guid", "audio", "started")
            )
        except (ValueError, KeyError, TypeError) as e:  # not JSON, or of another shape
            self.log(f"{self.marker.name} can't be read ({e!r}); removed")
            return
        rec = self.lib.record(slug)
        ep = (
            next(
                (e for e in rec["show"].episodes if (e.guid, e.audio) == (guid, audio)),
                None,
            )
            if rec
            else None
        )
        if ep and not ep.transcript:  # with one, the run died after saving it
            error = f"the last attempt, started {started}, stopped part-way (killed, e.g. out of memory, or crashed); not tried again"
            self.log(f"{slug}: {ep.title}: {error}")
            self.lib.update_episode(
                slug, ep, lambda ep: setattr(ep, "transcript_error", error)
            )

    def run(self) -> int:
        """Transcribe everything waiting, newest first, looking again after each episode;
        returns how many episodes were done. Hold transcribe.lock (main does)."""
        self.recover()
        done, tried = 0, set()
        while todo := [
            t for t in self.todo() if (t[0], t[1].guid, t[1].audio) not in tried
        ]:
            if self.paused():
                self.log(
                    f"transcribing is paused for now (PODCASTS_TRANSCRIBE_THREADS); {len(todo)} episodes wait"
                )
                break
            slug, ep, mode = todo[0]
            # Not twice in a run, should applying fail and leave it waiting.
            tried.add((slug, ep.guid, ep.audio))
            # A later sync may have dropped the feed or the episode; update_episode checks.
            path = self.lib.audio.path(ep.audio)
            self.log(f"{slug}: transcribing {ep.title}")
            _write_json(
                self.marker,
                {
                    "slug": slug,
                    "guid": ep.guid,
                    "audio": ep.audio,
                    "started": datetime.now(user_tz()).isoformat(timespec="seconds"),
                },
            )
            try:
                found = transcribe(
                    TranscriptionRequest(path, model=MODEL), self.transcriber
                )
            except (TranscribeError, OSError) as e:
                self.log(f"{slug}: couldn't transcribe {ep.title}: {e}")
                error = str(e)
                self.lib.update_episode(
                    slug,
                    ep,
                    lambda ep, error=error: setattr(ep, "transcript_error", error),
                )
                continue
            finally:
                # Only dying in Whisper counts against the episode, not in what follows.
                self.marker.unlink(missing_ok=True)
            # Before taking the sync lock: the model takes a while, and a sync can't start meanwhile.
            spans = self.find_ads(found.segments) if mode != "off" else []
            for a, b in spans:
                words = " ".join(s.text for s in found.segments if a <= s.start < b)
                self.log(f"  ad read at {clock(a)}-{clock(b)}: {words[:120]}")
            if self.lib.update_episode(
                slug, ep, partial(self._apply, slug, found, mode, spans)
            ):
                done += 1
        return done

    def _apply(
        self,
        slug: str,
        found: Transcript,
        mode: str,
        spans: list[tuple[float, float]],
        ep: Episode,
    ) -> None:
        """Save `found` as `ep`'s transcript, note the sponsor reads `spans` in it as its
        ad-read cuts (left out with mode "cut", only reported with "report"), and serve the
        episode as they make it."""
        self.store.save(slug, key(ep), ep, found.segments, found.language)
        if mode != "off":
            self.lib.audio.set_source(ep.audio, "ad-read", spans, active=mode == "cut")
        before = ep.ads_cut
        ep.transcript_error = ""
        if problem := self.lib.render(slug, ep, new_transcript=True):
            self.log(f"{slug}: {ep.title}: {problem}")
        if ep.ads_cut > before:
            self.log(
                f"{slug}: left {clock(ep.ads_cut - before)} of sponsor reads out of {ep.title}"
            )
        if ep.possible_ads:
            where = ", ".join(f"{clock(a)}-{clock(b)}" for a, b in ep.possible_ads)
            self.log(f"{slug}: possible ads in {ep.title} at {where}")


def main() -> None:
    os.nice(19)  # the LLM shares this CPU and answers people; it goes first
    lib = Library.from_env()
    with lib.only_one("transcribe.lock") as got:
        if not got:
            print("another transcription run is going", file=sys.stderr)
            return
        try:
            schedule = Schedule(os.environ.get("PODCASTS_TRANSCRIBE_THREADS", ""))
        except ValueError as e:
            sys.exit(str(e))

        def whisper(threads: int) -> WhisperTranscriber:
            print(
                f"loading Whisper on {threads} thread{'s' if threads != 1 else ''}",
                flush=True,
            )
            return WhisperTranscriber(
                models_dir("whisper"),
                lambda msg: print(msg, flush=True),
                load=partial(load_whisper, threads=threads),
            )

        transcriber = Scheduled(schedule, whisper)
        worker = Worker(
            lib,
            transcriber,
            log=lambda msg: print(msg, flush=True),
            chat=lib.chat,
            paused=lambda: not transcriber.threads(),
        )
        # A stop (systemctl, uv run hostctl units, a reboot) unwinds, so the marker goes with it;
        # only a kill (out of memory) leaves it behind. 143 is 128 + SIGTERM.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        print(f"transcribed {worker.run()} episodes", flush=True)


if __name__ == "__main__":
    main()
