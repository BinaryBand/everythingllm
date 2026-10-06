"""`news-audio`: the newest Daily News edition, read aloud, as an episode of a private feed.

Run by news-audio.timer after the edition job. It reads the newest edition of the news
site (sites' SiteStore), speaks its stories with Kokoro (podcasts.speech), encodes the result to MP3
and adds it to the `daily-news` feed, which Library keeps as a feed made here (local.json):
the sync never fetches or prunes it, and it keeps its newest KEEP episodes. An edition
already read aloud is skipped, so the timer can run more than once a day.

Speaking takes the CPU for a minute or two, at the lowest priority; it holds no lock until
the episode is published. The speech model is downloaded the first time there is something
to read.
"""

import html
import os
import sys
from datetime import date

from sites.build import Builder
from sites.store import SiteStore

from podcasts.library import Library, _now, clock, models_dir
from podcasts.rss import Episode, Show
from podcasts.speech import KokoroSynthesizer, Mp3Store, SpeechRequest, synthesize

SLUG = "daily-news"
SITE, SECTION = "news", "editions"
KEEP = 14
VOICE = "af_heart"


def script(title: str, day: str, sections: list[dict]) -> str:
    """The edition as text for `speak`, with pauses between stories and sections."""
    try:
        spoken_day = date.fromisoformat(day).strftime("%A, %B %-d")
    except ValueError:
        spoken_day = title

    def say(text: str) -> str:
        # Story text is plain; only <, > and & mean anything to speak's markup.
        text = html.escape(" ".join(str(text).split()), quote=False)
        return text if text[-1:] in ".!?" else text + "."

    parts = [f"Daily News for {spoken_day}.", '<break time="1s"/>']
    for section in sections:
        stories = [s for s in section.get("stories", []) if s.get("headline")]
        if not stories:
            continue
        parts += [say(section.get("name", "News")), '<break time="800ms"/>']
        for story in stories:
            parts.append(say(story["headline"]))
            if story.get("summary"):
                parts += ['<break time="400ms"/>', say(story["summary"])]
            parts.append('<break time="1.2s"/>')
    parts.append("That's the news.")
    return "\n".join(parts)


def describe(sections: list[dict]) -> str:
    items = "".join(
        f"<li>{html.escape(s['headline'])}</li>"
        for section in sections
        for s in section.get("stories", [])
        if s.get("headline")
    )
    return f"<ul>{items}</ul>" if items else ""


class NewsAudio:
    def __init__(self, lib: Library, sites: SiteStore, synth):
        self.lib, self.sites, self.synth = lib, sites, synth
        self.store = Mp3Store()

    def run(self) -> str:
        """Read the newest edition aloud unless that's done; returns what happened."""
        entries = self.sites.entries(SITE, SECTION)
        if not entries:
            return "no edition yet"
        entry, extra, _ = self.sites.get(SITE, SECTION, entries[0].slug)
        guid = f"{SITE}-{SECTION}-{entry.slug}"
        rec = self.lib.record(SLUG)
        if rec and any(e.guid == guid for e in rec["show"].episodes):
            return f"{entry.slug} is already read aloud"
        sections = extra.get("sections", [])
        audio = synthesize(
            SpeechRequest(script(entry.title, entry.date, sections), voice=VOICE),
            self.synth,
        )
        file = f"{entry.slug}.mp3"
        path = self.lib.site / SLUG / file
        self.store.write(audio, path)
        site = self.sites.site(SITE)
        ep = Episode(
            guid=guid,
            title=entry.title,
            url=entry.url,
            type="audio/mpeg",
            published=_now(),
            description=describe(sections),
            duration=clock(audio.seconds),
            file=file,
            bytes=path.stat().st_size,
        )
        show = Show(
            title=f"{site.title} (read aloud)",
            link=site.url,
            description=site.description,
        )
        self.lib.publish_local(SLUG, show, ep, f"{site.url}{SECTION}/", KEEP)
        return f"read {entry.slug} aloud: {clock(audio.seconds)}"


def main() -> None:
    os.nice(19)  # the LLM shares this CPU and answers people; it goes first
    paths = Builder.from_env()
    sites = SiteStore(paths.source, paths.content)
    lib = Library.from_env()
    with lib.only_one("news-audio.lock") as got:
        if not got:
            print("news-audio is already running", file=sys.stderr)
            return
        synth = KokoroSynthesizer(
            models_dir("kokoro"), on_download=lambda msg: print(msg, flush=True)
        )
        print(NewsAudio(lib, sites, synth).run(), flush=True)


if __name__ == "__main__":
    main()
