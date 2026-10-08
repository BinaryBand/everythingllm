import socket
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest
from sites import feeds, server, tools
from sites.feeds import (
    Feed,
    FeedError,
    Story,
    canonical,
    clean,
    dedupe,
    headlines,
    make_client,
    parse,
    pick,
)

NOW = datetime(2026, 10, 4, 3, 0, tzinfo=timezone.utc)


def ago(hours: float, now: datetime = NOW) -> datetime:
    return now - timedelta(hours=hours)


def rss(*items: tuple[str, str, float], extra: str = "", now: datetime = NOW) -> bytes:
    """items: (title, link, hours before `now`)."""
    body = "".join(
        f"""<item>
          <title>
             {title}
          </title>
          <link> {link} </link>
          <description>&lt;p&gt;About {title} &amp;amp; more.&lt;/p&gt; &lt;a href="{link}"&gt;Continue reading...&lt;/a&gt;</description>
          <pubDate>{format_datetime(ago(hours, now))}</pubDate>
          <media:title>not the headline</media:title>
        </item>"""
        for title, link, hours in items
    )
    return f"""<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>
<title>A paper</title><link>https://paper.example/</link>
{body}{extra}</channel></rss>""".encode()


ATOM = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xml:lang="sv" xmlns="http://www.w3.org/2005/Atom"><title type="text">Radio Sweden</title>
<link rel="alternate" href="https://radio.example/" />
<entry><id>rss:1</id><title type="text">Riksdag votes on budget</title>
<summary type="html">&lt;ul&gt;&lt;li&gt;&lt;p&gt;The vote is on Tuesday.&lt;/p&gt;&lt;/li&gt;&lt;/ul&gt;</summary>
<published>{ago(2).isoformat()}</published><updated>{ago(1).isoformat()}</updated>
<link rel="enclosure" href="https://radio.example/1.mp3" /><link href="https://radio.example/artikel/1" /></entry>
<entry><id>rss:2</id><title>No link</title><published>{ago(2).isoformat()}</published></entry>
</feed>""".encode()

RDF = b"""<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
  xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel rdf:about="https://dw.example/"><title>World</title><link>https://dw.example/</link></channel>
<item rdf:about="https://dw.example/a-1"><title>Peru fire kills 1</title>
<link>https://dw.example/en/peru/a-1?maca=en-rss-en-world-4025-rdf</link>
<description>One person has died.</description><dc:date>2026-10-04T02:08:00Z</dc:date></item>
</rdf:RDF>"""


class Web:
    def __init__(self, pages: dict[str, httpx.Response | bytes]):
        self.pages = pages
        self.gets: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.gets.append(str(request.url))
        page = self.pages.get(str(request.url), httpx.Response(404))
        return httpx.Response(200, content=page) if isinstance(page, bytes) else page

    def client(self) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(self.handler), follow_redirects=True
        )


@pytest.fixture
def section(monkeypatch):
    """A "Test" section with three feeds, replacing the real ones."""
    test = [
        Feed("Paper A", "https://a.example/rss"),
        Feed("Paper B", "https://b.example/rss"),
        Feed("Radio", "https://radio.example/atom"),
    ]
    monkeypatch.setattr(feeds, "FEEDS", {"Test": test})
    return test


def test_rss_items_are_cleaned_and_dated():
    data = rss(
        ("Budget &amp; tax", "https://a.example/1?utm_source=rss&amp;id=7#top", 2),
        ("Old", "https://a.example/old", 3),
        extra="<item><title>Undated</title><link>https://a.example/u</link></item>",
    )
    stories = parse(data, "Paper A")
    assert [s.title for s in stories] == [
        "Budget & tax",
        "Old",
    ]  # newest first; undated left out
    s = stories[0]
    assert s.url == "https://a.example/1?id=7"
    assert s.summary == "About Budget & tax & more. Continue reading..."
    assert (s.source, s.published) == ("Paper A", ago(2))


def test_guid_is_the_link_unless_it_says_otherwise():
    items = f"""<item><title>Has guid</title><guid>https://a.example/g</guid><pubDate>{format_datetime(ago(1))}</pubDate></item>
    <item><title>Not a link</title><guid isPermaLink="false">https://a.example/x</guid><pubDate>{format_datetime(ago(1))}</pubDate></item>"""
    assert [(s.title, s.url) for s in parse(rss(extra=items), "A")] == [
        ("Has guid", "https://a.example/g")
    ]


def test_atom_uses_the_alternate_link_and_published_date():
    [s] = parse(ATOM, "Radio Sweden")
    assert (s.title, s.url, s.summary, s.published) == (
        "Riksdag votes on budget",
        "https://radio.example/artikel/1",
        "The vote is on Tuesday.",
        ago(2),
    )


def test_rdf_reads_dublin_core_dates():
    [s] = parse(RDF, "DW")
    assert s.url == "https://dw.example/en/peru/a-1"
    assert s.published == datetime(2026, 10, 4, 2, 8, tzinfo=timezone.utc)


def test_not_a_feed():
    with pytest.raises(FeedError, match="not valid XML"):
        parse(b"<html><body>Sorry<br></body></html>", "A")
    with pytest.raises(FeedError, match="not an RSS or Atom feed"):
        parse(b"<html><body>Sorry</body></html>", "A")


def test_clean_caps_at_a_word():
    assert clean("<p>one  two\nthree</p>") == "one two three"
    assert clean("alpha beta, gamma delta", 14) == "alpha beta…"


def test_canonical_keeps_real_parameters():
    assert (
        canonical("https://x.example/a?id=1&at_medium=RSS&traffic_source=rss#c")
        == "https://x.example/a?id=1"
    )
    # What's kept stays as it was.
    for url in ("https://e.example/a?12345", "https://e.example/a?x=%E4&q=a+b"):
        assert canonical(url) == url


def story(title: str, url: str, hours: float, source: str = "A") -> Story:
    return Story(title, "", source, url, ago(hours))


def test_dedupe_by_link_and_near_identical_headline():
    stories = [
        story("Storm hits the coast", "https://a.example/1", 1),
        story("Storm hits the coast!", "https://b.example/9", 1),
        story("Something else", "https://a.example/1", 2),
        story("Storm hits the east coast hard", "https://b.example/2", 1),
    ]
    assert [s.url for s in dedupe(stories)] == [
        "https://a.example/1",
        "https://b.example/2",
    ]


def test_pick_gives_each_feed_a_share_then_fills():
    busy = [story(f"busy {i}", f"https://a.example/{i}", i / 10) for i in range(20)]
    quiet = [story("quiet", "https://b.example/1", 20)]
    picked = pick([busy, quiet], limit=5)
    assert [s.title for s in picked] == [
        "busy 0",
        "busy 1",
        "busy 2",
        "busy 3",
        "quiet",
    ]
    assert picked == sorted(picked, key=lambda s: s.published, reverse=True)


def test_headlines_merges_recent_stories_and_names_failed_feeds(section):
    web = Web(
        {
            "https://a.example/rss": rss(
                ("Storm hits the coast", "https://a.example/storm", 1),
                ("Last year's story", "https://a.example/old", 24 * 365),
                ("Yesterday morning", "https://a.example/y", 29),
            ),
            "https://b.example/rss": rss(
                ("Storm hits the coast.", "https://b.example/storm", 0.5),
                ("Court ruling", "https://b.example/court", 5),
                ("Too old", "https://b.example/too-old", 31),
            ),
        }
    )
    with web.client() as client:
        stories, failed = headlines(client, "test", NOW)
    assert [(s.title, s.source) for s in stories] == [
        ("Storm hits the coast", "Paper A"),
        ("Court ruling", "Paper B"),
        ("Yesterday morning", "Paper A"),
    ]
    assert failed == ["Radio (HTTP 404)"]
    assert len(web.gets) == 3


def test_headlines_reports_a_bad_feed_without_failing(section):
    web = Web(
        {
            "https://a.example/rss": b"<html>busy</html>",
            # Not a FeedError: an encoding Python doesn't know.
            "https://b.example/rss": b'<?xml version="1.0" encoding="foo"?><rss/>',
            "https://radio.example/atom": ATOM,
        }
    )
    with web.client() as client:
        stories, failed = headlines(client, "Test", NOW)
    assert [s.source for s in stories] == ["Radio"]
    assert failed == [
        "Paper A (not an RSS or Atom feed.)",
        "Paper B (unknown encoding: foo)",
    ]


def test_slow_feeds_are_skipped_at_the_deadline(section):
    def slow():
        for _ in range(100):
            time.sleep(0.02)
            yield b" "

    web = Web(
        {
            "https://a.example/rss": httpx.Response(200, content=slow()),
            "https://b.example/rss": rss(("Quick", "https://b.example/q", 1)),
            "https://radio.example/atom": ATOM,
        }
    )
    with web.client() as client:
        started = time.monotonic()
        stories, failed = headlines(client, "Test", NOW, seconds=0.3)
    assert time.monotonic() - started < 1
    assert [s.title for s in stories] == ["Quick", "Riksdag votes on budget"]
    assert failed == ["Paper A (no answer in 0.3 s)"] or failed == [
        "Paper A (too slow)"
    ]


def test_unknown_section():
    with (
        httpx.Client() as client,
        pytest.raises(FeedError, match="use one of: US, Sweden, World"),
    ):
        headlines(client, "Norway", NOW)


def test_feeds_on_private_addresses_are_refused(section, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    with make_client() as client:
        stories, failed = headlines(client, "Test", NOW)
    assert stories == [] and len(failed) == 3
    assert "private or local address" in failed[0]


def test_tool_output_is_compact_text(section, monkeypatch):
    web = Web(
        {
            "https://a.example/rss": rss(
                ("Court ruling", "https://a.example/court", 1),
                now=datetime.now(timezone.utc),
            )
        }
    )
    monkeypatch.setattr(feeds, "make_client", web.client)
    out = tools.headlines("test")
    lines = out.splitlines()
    assert lines[0] == "Test: 1 stories from the last 30 hours, newest first."
    assert lines[1] == "1. Court ruling"
    assert lines[2].startswith("   Paper A | 2026-") and lines[2].endswith(
        " UTC | https://a.example/court"
    )
    assert lines[3] == "   About Court ruling & more. Continue reading..."
    assert (
        lines[4]
        == "Skipped feeds that didn't load: Paper B (HTTP 404); Radio (HTTP 404)."
    )


def test_tool_with_no_stories():
    assert (
        tools.render_headlines("World", [], [])
        == "World: no stories from the last 30 hours."
    )


def test_real_feed_list_is_well_formed():
    assert list(feeds.FEEDS) == ["US", "Sweden", "World"] == list(server.SECTIONS)
    for section_feeds in feeds.FEEDS.values():
        assert 3 <= len(section_feeds) <= 8
        assert all(f.url.startswith("https://") and f.name for f in section_feeds)
