import time

import httpx
import pytest
from podcasts.find import feed_links, find
from podcasts.library import clean_url
from podcasts.rss import FeedError
from test_library import feed, rss

GOOD = "https://feeds.example/good"
EMPTY = "https://feeds.example/empty"
SLOW = "https://feeds.example/slow"


class Web:
    def __init__(self, pages: dict[str, httpx.Response]):
        self.pages = pages
        self.gets: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.gets.append(str(request.url))
        key = f"{request.url.scheme}://{request.url.host}{request.url.path}"
        return self.pages.get(key, httpx.Response(404))

    def client(self) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(self.handler), follow_redirects=True
        )


def apple(*feeds: str) -> httpx.Response:
    results = [{"collectionName": "x", "feedUrl": f} for f in feeds] + [
        {"collectionName": "no feed"}
    ]
    return httpx.Response(200, json={"resultCount": len(results), "results": results})


FEEDS = {GOOD: rss(feed(("a", "1"), ("b", "2"))), EMPTY: rss(feed())}


def test_search_by_name_checks_each_feed_and_lists_working_first():
    web = Web(
        {
            "https://itunes.apple.com/search": apple(
                EMPTY, GOOD, "https://feeds.example/gone"
            ),
            **FEEDS,
        }
    )
    with web.client() as client:
        found, _note = find(client, "the show")
    assert "term=the+show" in web.gets[0] and "media=podcast" in web.gets[0]
    assert [(f.url, f.show is not None) for f in found] == [
        (GOOD, True),
        (EMPTY, False),
        ("https://feeds.example/gone", False),
    ]
    show = found[0].show
    assert show is not None and show.title == "The Show"
    assert "no episodes" in found[1].error and "404" in found[2].error


def test_search_with_no_results_explains():
    web = Web({"https://itunes.apple.com/search": apple()})
    with web.client() as client:
        assert find(client, "nothing") == (
            [],
            (
                "Apple's podcast directory has nothing for 'nothing'. "
                "Try another spelling, or the show's website."
            ),
        )


def test_apple_link_is_looked_up_by_id():
    web = Web({"https://itunes.apple.com/lookup": apple(GOOD), **FEEDS})
    with web.client() as client:
        found, _ = find(
            client, "https://podcasts.apple.com/us/podcast/the-show/id12345?i=99"
        )
    assert "id=12345" in web.gets[0]
    assert [f.url for f in found] == [GOOD]


def test_app_pages_are_explained():
    page = httpx.Response(
        200,
        text="<html><title>Show</title></html>",
        headers={"content-type": "text/html"},
    )
    with Web({"https://open.spotify.com/show/abc": page}).client() as client:
        found, note = find(client, "https://open.spotify.com/show/abc")
    assert found == []
    assert "Spotify" in note and "search by the show's name" in note


def test_feed_url_is_returned_as_is():
    web = Web(FEEDS)
    with web.client() as client:
        found, _ = find(client, GOOD)
    assert [f.url for f in found] == [GOOD] and found[0].show
    assert web.gets == [GOOD]  # fetched once


@pytest.mark.parametrize(
    "pasted",
    [
        "https://feeds.example/show.rss](https://feeds.example/show.rss",  # a Markdown link, mangled
        "<https://feeds.example/show.rss>",
        " https://feeds.example/show.rss. ",
        "(https://feeds.example/show.rss)",
    ],
)
def test_pasted_urls_are_cleaned(pasted):
    assert clean_url(pasted) == "https://feeds.example/show.rss"


def test_mangled_markdown_link_finds_the_feed():
    web = Web(FEEDS)
    with web.client() as client:
        found, _ = find(client, f"{GOOD}]({GOOD}")
    assert [f.url for f in found] == [GOOD]


def test_web_page_feed_links_are_followed():
    page = f"""<html><head>
    <link rel="alternate" type="application/rss+xml" href="/podcast.xml">
    <link rel="Alternate" type="application/rss+xml" href="{GOOD}">
    <link rel="alternate" type="application/atom+xml" href="/atom.xml">
    <link rel="stylesheet" type="text/css" href="/style.css">
    </head></html>"""
    web = Web(
        {
            "https://show.example/": httpx.Response(
                200, text=page, headers={"content-type": "text/html"}
            ),
            "https://show.example/podcast.xml": FEEDS[GOOD],
            **FEEDS,
        }
    )
    with web.client() as client:
        found, _ = find(client, "https://show.example/")
    assert [f.url for f in found] == ["https://show.example/podcast.xml", GOOD]


def test_page_without_feed_links_explains():
    web = Web(
        {
            "https://show.example/": httpx.Response(
                200, text="<html></html>", headers={"content-type": "text/html"}
            )
        }
    )
    with web.client() as client:
        assert find(client, "https://show.example/")[1].startswith(
            "That URL isn't a feed and doesn't link to one."
        )


def test_unreachable_page_raises():
    with Web({}).client() as client, pytest.raises(FeedError, match="404"):
        find(client, "https://show.example/missing")


def test_feed_links_dedupes_and_resolves():
    html = '<link rel="alternate" type="application/rss+xml" href="a.xml"><link rel="alternate" type="application/rss+xml" href="/x/a.xml">'
    assert feed_links(html, "https://h.example/x/") == ["https://h.example/x/a.xml"]


def test_apple_sends_garbage():
    web = Web(
        {
            "https://itunes.apple.com/search": httpx.Response(
                200, text="<html>busy</html>"
            )
        }
    )
    with web.client() as client, pytest.raises(FeedError, match="isn't JSON"):
        find(client, "x")


def test_slow_feeds_stop_at_the_deadline():
    def slow():
        for _ in range(100):
            time.sleep(0.02)
            yield b" "

    web = Web(
        {
            "https://itunes.apple.com/search": apple(GOOD, SLOW),
            **FEEDS,
            SLOW: httpx.Response(200, content=slow()),
        }
    )
    with web.client() as client:
        started = time.monotonic()
        found, _ = find(client, "the show", time.monotonic() + 0.3)
    assert time.monotonic() - started < 1
    assert [(f.url, f.show is not None) for f in found] == [(GOOD, True), (SLOW, False)]
