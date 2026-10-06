import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest
from podcasts import news_audio, tools
from podcasts.library import LibraryError
from podcasts.news_audio import NewsAudio, script
from podcasts.speech import Audio
from test_library import Remote, feed, served, subscribe

SECTIONS = [
    {
        "name": "US",
        "stories": [
            {"headline": "Rates <held> & steady", "summary": "The Fed held rates"},
            {"headline": "Second story", "summary": ""},
        ],
    },
    {"name": "Empty", "stories": []},
]


def news_audio_for(lib, sites: "FakeSites", synth) -> NewsAudio:
    """A NewsAudio reading `sites` as its SiteStore."""
    return NewsAudio(lib, sites, synth)  # ty: ignore[invalid-argument-type]


class FakeSites:
    def __init__(self, days):
        self.days = days

    def entries(self, site, section):
        return [SimpleNamespace(slug=d) for d in sorted(self.days, reverse=True)]

    def get(self, site, section, slug):
        entry = SimpleNamespace(
            slug=slug,
            title=f"Daily News — {slug}",
            date=slug,
            url=f"https://h/news/editions/{slug}/",
        )
        return entry, {"sections": SECTIONS}, ""

    def site(self, name):
        return SimpleNamespace(
            title="Daily News", url="https://h/news/", description="Headlines"
        )


class FakeSynth:
    def __init__(self):
        self.said = []

    def synthesize(self, req):
        self.said.append(req.text)
        return Audio(b"\x00\x00" * 2400, 24000)  # 0.1 s each


def test_script_escapes_and_paces():
    text = script("t", "2026-10-04", SECTIONS)
    assert text.startswith("Daily News for Sunday, October 4.")
    assert "Rates &lt;held&gt; &amp; steady." in text
    assert "The Fed held rates." in text
    assert "Empty" not in text
    assert text.endswith("That's the news.")


def test_reads_the_newest_edition_once(lib, tmp_path):
    synth = FakeSynth()
    news = news_audio_for(lib, FakeSites(["2026-10-03", "2026-10-04"]), synth)
    assert news.run().startswith("read 2026-10-04 aloud")
    assert synth.said[0] == "Daily News for Sunday, October 4."
    assert news.run() == "2026-10-04 is already read aloud"

    folder = tmp_path / "site" / "daily-news"
    assert sorted(p.name for p in folder.iterdir()) == ["feed.xml"]
    assert served(lib, "daily-news") == [
        "2026-10-04.mp3"
    ]  # from the audio store, as downloads are
    item = ET.fromstring((folder / "feed.xml").read_bytes()).find("channel/item")
    assert item is not None
    assert item.findtext("title") == "Daily News — 2026-10-04"
    enc = item.find("enclosure")
    assert enc is not None
    assert enc.get("url") == (
        "https://host.ts.net:8445/podcasts/daily-news/2026-10-04.mp3"
    )
    assert "Rates &lt;held&gt;" in item.findtext("description", "")
    assert "daily-news" in lib.local_feeds() and "daily-news" not in lib.feeds()
    assert "daily-news/feed.xml" in (tmp_path / "site" / "index.html").read_text()


def test_keeps_the_newest(lib, tmp_path, monkeypatch):
    monkeypatch.setattr(news_audio, "KEEP", 2)
    sites = FakeSites([])
    news = news_audio_for(lib, sites, FakeSynth())
    for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
        sites.days.append(day)
        news.run()
    eps = lib.record("daily-news")["show"].episodes
    assert [e.file for e in eps] == ["2026-10-03.mp3", "2026-10-02.mp3"]
    assert sorted(p.name for p in (tmp_path / "site" / "daily-news").iterdir()) == [
        "feed.xml"
    ]
    lib.gc(grace=0)
    assert served(lib, "daily-news") == ["2026-10-02.mp3", "2026-10-03.mp3"]
    # FakeSynth says every edition the same, so both are one original, kept once.
    assert len(list((tmp_path / "state" / "audio").iterdir())) == 1


def test_sync_leaves_local_feeds_alone(lib, tmp_path, monkeypatch):
    news_audio_for(lib, FakeSites(["2026-10-04"]), FakeSynth()).run()
    remote = Remote(feed(("a", "1")))
    subscribe(lib, remote)
    with remote.client() as client:
        lib.sync(client, "daily-news")
    assert "https://h/news/editions/" not in remote.gets
    assert served(lib, "daily-news") == ["2026-10-04.mp3"]
    assert lib.audio.path(lib.record("daily-news")["show"].episodes[0].audio).is_file()
    with pytest.raises(LibraryError, match="made on this server"):
        lib.remove("daily-news")
    monkeypatch.setattr(tools, "lib", lambda: lib)
    assert (
        "made on this server from https://h/news/editions/, keeping the newest 14"
        in tools.list_podcasts()
    )


def test_no_edition(lib):
    assert news_audio_for(lib, FakeSites([]), FakeSynth()).run() == "no edition yet"
