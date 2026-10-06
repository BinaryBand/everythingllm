import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import time
from pathlib import Path

import pytest
from llm import LLMError
from podcasts import ad_reads, tools
from podcasts.ad_reads import AdReadError, find_ad_reads
from podcasts.files import _write_json
from podcasts.rss import PODCAST
from podcasts.search import search
from podcasts.segments import Segment, retime
from podcasts.sponsor_text import sponsor_spans
from podcasts.transcripts import Schedule, Scheduled, Worker
from podcasts.whisper import Stream, TranscriptionError
from test_avio import make_mp3
from test_library import FEED_URL, feed, subscribe
from test_scrub import Mp3Remote as Mp3RemoteShort

READ = [
    Segment(0.0, 5.0, "Welcome back to the show."),
    Segment(60.0, 64.0, "This episode is brought to you by Mattress Co."),
    Segment(64.0, 80.0, "I sleep so well on it, honestly."),
    Segment(80.0, 90.0, "Go to mattressco.com/show and use code SHOW for 20% off."),
    Segment(120.0, 126.0, "Anyway, back to the Tylenol murders."),
    Segment(400.0, 404.0, "Check out our website dot com, which has nothing."),
]


def test_sponsor_spans_need_several_phrases_close_together():
    assert sponsor_spans(READ) == [(60.0, 90.0)]
    assert sponsor_spans(READ[:2] + READ[4:]) == []  # one phrase alone isn't a read


def test_sponsor_spans_ignore_too_long_stretches():
    talk = [Segment(i * 50.0, i * 50.0 + 5, "use code X") for i in range(6)]  # 255 s
    assert sponsor_spans(talk) == []


def test_retime_drops_cut_segments_and_moves_later_ones():
    out = retime(READ, [(60.0, 90.0)])
    assert [(s.start, s.text[:8]) for s in out] == [
        (0.0, "Welcome "),
        (90.0, "Anyway, "),
        (370.0, "Check ou"),
    ]


class FakeTranscriber:
    def __init__(self, segments=READ, fail=False):
        self.segments, self.fail, self.heard = segments, fail, []

    def transcribe(self, req):
        self.heard.append(req.audio)
        if self.fail:
            raise TranscriptionError("cannot read it")
        return Stream("en", 754.0, iter(self.segments))


SECONDS = (
    130  # past every segment of READ that matters: the read at 60-90, "Anyway" at 120
)
LONG = make_mp3(SECONDS)


def Remote(body: bytes):
    return Mp3RemoteShort(body, LONG)


@pytest.fixture
def lib(lib):
    subscribe(lib, Remote(feed(("a", "1"), ("b", "2"))))
    return lib


def worker(lib, transcriber=None):
    return Worker(lib, transcriber or FakeTranscriber(), log=lambda msg: None)


def guids(lib, heard) -> list[str]:
    """Which episodes the transcriber heard: it is given the originals."""
    by_audio = {e.audio: e.guid for e in lib.record("the-show")["show"].episodes}
    return [by_audio.get(p.name, p.name) for p in heard]


def set_ad_words(lib, mode):
    with Remote(feed(("a", "1"), ("b", "2"))).client() as client:
        lib.add(client, FEED_URL, ad_words=mode)


def test_transcripts_are_written_linked_and_reported(lib, tmp_path):
    set_ad_words(lib, "report")
    w = worker(lib)
    assert w.run() == 2
    assert guids(lib, w.transcriber.heard) == ["b", "a"]  # newest first
    assert all(
        p.parent == tmp_path / "state" / "audio" for p in w.transcriber.heard
    )  # the originals
    folder = tmp_path / "site" / "the-show"
    eps = lib.record("the-show")["show"].episodes
    assert all(e.transcript == e.file.rsplit(".", 1)[0] + ".vtt" for e in eps)
    assert (
        (folder / eps[0].transcript)
        .read_text()
        .startswith("WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nWelcome")
    )
    assert eps[0].possible_ads == [[60.0, 90.0]]
    assert eps[0].ads_cut == 0
    tag = ET.fromstring((folder / "feed.xml").read_bytes()).find(
        f"channel/item/{{{PODCAST}}}transcript"
    )
    assert tag is not None
    assert tag.attrib == {
        "url": f"https://host.ts.net:8445/podcasts/the-show/{eps[0].transcript}",
        "type": "text/vtt",
    }
    saved = json.loads(
        next(
            (tmp_path / "state" / "transcripts" / "the-show").glob("*.json")
        ).read_text()
    )
    assert saved["segments"][1] == [
        60.0,
        64.0,
        "This episode is brought to you by Mattress Co.",
    ]
    assert w.run() == 0  # nothing left to do


def test_sync_keeps_transcripts(lib, tmp_path):
    set_ad_words(lib, "report")
    worker(lib).run()
    with Remote(feed(("a", "1"), ("b", "2"))).client() as client:
        lib.sync(client)
    eps = lib.record("the-show")["show"].episodes
    assert all(
        e.transcript and (tmp_path / "site" / "the-show" / e.transcript).is_file()
        for e in eps
    )
    assert eps[0].possible_ads == [[60.0, 90.0]]


def test_ad_words_cut_cuts_and_retimes(lib, tmp_path):  # the default
    before = lib.record("the-show")["show"].episodes[0]
    worker(lib).run()
    ep = lib.record("the-show")["show"].episodes[0]
    m = lib.audio.manifest("the-show", ep.file)
    assert (ep.ads_cut, ep.bytes, ep.duration, ep.possible_ads) == (
        30.0,
        m.size,
        "1:40",
        [],
    )
    assert (
        ep.file != before.file and ep.transcript == ep.file.rsplit(".", 1)[0] + ".vtt"
    )
    assert [(c["start"], c["end"], c["source"]) for c in lib.audio.cuts(ep.audio)] == [
        (60.0, 90.0, "ad-read")
    ]
    vtt = (tmp_path / "site" / "the-show" / ep.transcript).read_text()
    assert "Mattress" not in vtt
    # 30 s earlier, give or take the frame (26 ms) the cut ends on.
    assert "00:01:29.98" in vtt.split("\nAnyway")[0].splitlines()[-1]
    # The transcript is kept in the original's times, for whatever cuts come later.
    saved = json.loads(
        next(
            (tmp_path / "state" / "transcripts" / "the-show").glob("*.json")
        ).read_text()
    )
    assert [60.0, 64.0, "This episode is brought to you by Mattress Co."] in saved[
        "segments"
    ]
    assert (
        "at 1:30: Anyway, back to the Tylenol murders." in search(lib, "tylenol")[0][0]
    )


def test_ad_words_off_reports_nothing(lib):
    set_ad_words(lib, "off")
    worker(lib).run()
    assert all(
        e.possible_ads == [] and e.transcript
        for e in lib.record("the-show")["show"].episodes
    )


def test_failure_is_recorded_and_not_retried(lib):
    w = worker(lib, transcriber=FakeTranscriber(fail=True))
    assert w.run() == 0
    eps = lib.record("the-show")["show"].episodes
    assert [e.transcript_error for e in eps] == ["cannot read it"] * 2
    assert w.todo() == []


def test_an_episode_a_run_died_on_is_marked_and_not_tried_again(lib):
    """A run killed mid-episode (out of memory) leaves its marker; the next run marks that
    episode failed instead of trying it, and dying, every time."""
    w = worker(lib)
    died_on = next(e for e in lib.record("the-show")["show"].episodes if e.guid == "b")
    _write_json(
        w.marker,
        {
            "slug": "the-show",
            "guid": "b",
            "audio": died_on.audio,
            "started": "2026-10-05T13:37:06",
        },
    )
    assert w.run() == 1
    assert guids(lib, w.transcriber.heard) == ["a"]
    eps = {e.guid: e for e in lib.record("the-show")["show"].episodes}
    assert eps["b"].transcript_error.startswith(
        "the last attempt, started 2026-10-05T13:37:06, stopped part-way"
    )
    assert eps["a"].transcript and not w.marker.exists()


def test_the_marker_is_gone_after_a_recorded_failure(lib):
    w = worker(lib, transcriber=FakeTranscriber(fail=True))
    w.run()
    assert not w.marker.exists()


class Stopped(FakeTranscriber):
    def transcribe(self, req):
        raise SystemExit(143)  # what main makes of SIGTERM


def test_a_stopped_run_takes_its_marker_with_it(lib):
    """A stop (systemctl, make units, a reboot) isn't a death: the episode waits as before."""
    w = worker(lib, transcriber=Stopped())
    with pytest.raises(SystemExit):
        w.run()
    assert not w.marker.exists()
    assert not any(e.transcript_error for e in lib.record("the-show")["show"].episodes)


def test_dying_after_whisper_does_not_count_against_the_episode(lib, monkeypatch):
    w = worker(lib)
    monkeypatch.setattr(w, "find_ads", lambda segments: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        w.run()
    assert not w.marker.exists()


@pytest.mark.parametrize(
    "text", ['{"slug": "the-show", "gu', '{"slug": "the-show"}', "[1, 2]"]
)
def test_a_marker_that_cannot_be_read_is_removed_and_the_run_goes_on(lib, text):
    w = worker(lib)
    w.marker.write_text(text)
    assert w.run() == 2
    assert not w.marker.exists()


def test_a_marker_for_an_episode_already_transcribed_is_only_removed(lib):
    """The run died after saving the transcript: nothing went wrong with the episode."""
    w = worker(lib)
    assert w.run() == 2
    ep = next(e for e in lib.record("the-show")["show"].episodes if e.guid == "b")
    _write_json(
        w.marker,
        {
            "slug": "the-show",
            "guid": "b",
            "audio": ep.audio,
            "started": "2026-10-05T13:37:06",
        },
    )
    w.run()
    ep = next(e for e in lib.record("the-show")["show"].episodes if e.guid == "b")
    assert ep.transcript and not ep.transcript_error and not w.marker.exists()


def test_episode_changed_meanwhile_is_left_alone(lib, tmp_path):
    ep = lib.record("the-show")["show"].episodes[0]
    assert not lib.update_episode(
        "the-show", replace(ep, bytes=ep.bytes + 1), lambda e: None
    )
    assert lib.update_episode("the-show", ep, lambda e: None)


def test_episodes_from_before_originals_wait_for_the_sync(lib):
    rec = lib.record("the-show")
    for e in rec["show"].episodes:
        e.audio = ""
    lib._save_record("the-show", rec["show"])
    assert worker(lib).todo() == []


def test_transcribe_off_skips_the_feed(lib):
    with Remote(feed(("a", "1"), ("b", "2"))).client() as client:
        lib.add(client, FEED_URL, transcribe=False)
    assert worker(lib).todo() == []


def test_search(lib, monkeypatch):
    set_ad_words(lib, "report")
    worker(lib).run()
    lines, total, searched = search(lib, "tylenol MURDERS")
    assert (total, searched) == (2, 2)
    assert lines[0].startswith(
        "- The Show — Ep b & more (2026-10-02) at 2:00: Anyway, back to the Tylenol"
    )
    assert search(lib, "mattress use code")[1] == 2  # every word, across segments
    assert search(lib, "bigfoot")[:2] == ([], 0)

    monkeypatch.setattr(tools, "lib", lambda: lib)
    assert "at 2:00" in tools.search_podcasts("Tylenol")
    assert (
        tools.search_podcasts("bigfoot") == "Not found in the 2 transcribed episodes."
    )
    listed = tools.list_podcasts()
    assert "transcribed; possible sponsor reads at 1:00-1:30" in listed
    assert "cutting ads, transcribing, reporting ad reads" in listed


def chat_answering(*answers):
    calls = []

    def chat(messages):
        calls.append(messages[1]["content"])
        return answers[len(calls) - 1]

    chat.calls = calls  # ty: ignore[unresolved-attribute]
    return chat


def test_find_ad_reads_maps_lines_to_times():
    chat = chat_answering(
        '```json\n{"ads": [{"first": 1, "last": 3, "what": "Mattress Co"}]}\n```'
    )
    assert find_ad_reads(chat, READ, log=lambda m: None) == [(60.0, 90.0)]
    assert (
        chat.calls[0].splitlines()[1]
        == "[1] 1:00 This episode is brought to you by Mattress Co."
    )


def test_find_ad_reads_checks_the_answer(monkeypatch):
    logged = []
    chat = chat_answering(
        '{"ads": [{"first": 3, "last": 1}, {"first": 0, "last": 99}, {"first": 1, "last": 1}, "x"]}'
    )
    assert find_ad_reads(chat, READ, log=logged.append) == []
    assert len(logged) == 4  # backwards, out of range, too short (4 s), not an object
    with pytest.raises(AdReadError):  # asked once more for the JSON, then given up on
        find_ad_reads(chat_answering("I found no ads.", "Still none."), READ)
    assert find_ad_reads(chat_answering("None.", '{"ads": []}'), READ) == []


def test_find_ad_reads_chunks_and_joins_across_the_boundary(monkeypatch):
    monkeypatch.setattr(ad_reads, "CHUNK", 3)
    chat = chat_answering(
        '{"ads": [{"first": 1, "last": 2}]}', '{"ads": [{"first": 3, "last": 3}]}'
    )
    assert find_ad_reads(chat, READ, log=lambda m: None) == [(60.0, 90.0)]
    assert chat.calls[1].startswith("[3] 1:20 Go to")


def test_worker_cuts_what_the_model_finds(lib, tmp_path):
    chat = chat_answering(*['{"ads": [{"first": 4, "last": 4, "what": "x"}]}'] * 2)
    Worker(lib, FakeTranscriber(), log=lambda m: None, chat=chat).run()
    ep = lib.record("the-show")["show"].episodes[0]
    assert (ep.ads_cut, ep.possible_ads) == (6.0, [])
    assert "Anyway" not in (tmp_path / "site" / "the-show" / ep.transcript).read_text()


def test_worker_falls_back_to_phrases_when_the_model_fails(lib):
    def broken(messages):
        raise LLMError("DeepSeek answered 500")

    logged = []
    Worker(lib, FakeTranscriber(), log=logged.append, chat=broken).run()
    assert (
        lib.record("the-show")["show"].episodes[0].ads_cut == 30.0
    )  # the phrase list's read
    assert any("using the phrase list" in m for m in logged)


def test_a_new_episode_goes_ahead_of_the_backlog(lib):
    class Arriving(FakeTranscriber):
        def transcribe(self, req):
            if len(self.heard) == 0:  # c comes out while b is being transcribed
                with Remote(
                    feed(("a", "1"), ("b", "2"), ("c", "3"))
                ).client() as client:
                    lib.sync(client)
            return super().transcribe(req)

    w = worker(lib, Arriving())
    assert w.run() == 3
    assert guids(lib, w.transcriber.heard) == ["b", "c", "a"]


def test_an_episode_that_cant_be_saved_isnt_tried_again_in_the_run(lib, tmp_path):
    class Removing(FakeTranscriber):
        def transcribe(self, req):
            req.audio.unlink()  # gone before the result is saved: it stays waiting
            return super().transcribe(req)

    w = worker(lib, Removing())
    assert w.run() == 0
    assert len(w.transcriber.heard) == 2


def test_paused_stops_the_run(lib):
    logged = []
    w = Worker(lib, FakeTranscriber(), log=logged.append, paused=lambda: True)
    assert w.run() == 0
    assert w.transcriber.heard == []
    assert "2 episodes wait" in logged[0]


def test_schedule_gives_threads_by_time_of_day():
    s = Schedule("22:00=1, 8:00=4")
    assert [s.threads(time(h)) for h in (0, 7, 8, 21, 22, 23)] == [1, 1, 4, 4, 1, 1]
    assert Schedule("3").threads(time(12)) == 3
    assert Schedule("").threads(time(12)) == 1
    for bad in ("four", "8:00=x", "25:00=1", "8:00=-1"):
        with pytest.raises(ValueError):
            Schedule(bad)


def test_scheduled_loads_whisper_again_when_the_threads_change():
    now, made = [time(9)], []
    scheduled = Scheduled(
        Schedule("08:00=4,22:00=1"),
        lambda n: made.append(n) or FakeTranscriber(),
        lambda: now[0],
    )
    req = type("Req", (), {"audio": Path("x.mp3")})()
    scheduled.transcribe(req)
    scheduled.transcribe(req)
    now[0] = time(23)
    scheduled.transcribe(req)
    assert made == [4, 1]
