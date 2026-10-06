"""Find a podcast's RSS feed from its name, an Apple Podcasts link or its website.

Sources, most reliable first:
  - Apple's podcast directory (the iTunes Search API, no key): search by name, or look
    up the id in a podcasts.apple.com link. Nearly every public show is listed, with its
    feed URL.
  - The URL itself, if it's already a feed.
  - A web page's feed links (<link rel="alternate" type="application/rss+xml">), which
    podcast hosts put on show pages.

Every candidate is fetched and parsed before it's returned, so the model only sees
feeds that work.
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from podcasts.library import clean_url, follow_move, get_feed, permanent_url, read
from podcasts.rss import FeedError, Show, parse

APPLE_SEARCH = "https://itunes.apple.com/search"
APPLE_LOOKUP = "https://itunes.apple.com/lookup"
APPLE_ID_RE = re.compile(r"/id(\d+)")
MAX_RESULTS = 5
CHECK_SECONDS = (
    40  # all checks together, inside the 60 s a tool call may take (without a deadline)
)


@dataclass
class Found:
    url: str  # where to subscribe
    show: Show | None = None
    error: str = ""


def _found(show: Show, url: str) -> Found:
    if not show.episodes:
        return Found(url, error="the feed has no episodes with audio or video.")
    return Found(url, show)


def _check(client: httpx.Client, url: str, deadline: float | None) -> Found:
    try:
        show, current, _ = get_feed(client, url, deadline)
    except FeedError as e:
        return Found(url, error=str(e))
    return _found(show, current)


def _check_all(
    client: httpx.Client, urls: list[str], deadline: float | None
) -> list[Found]:
    """Working feeds first (otherwise in the given order); slow ones are reported as such."""
    seconds = CHECK_SECONDS if deadline is None else max(0, deadline - time.monotonic())
    pool = ThreadPoolExecutor(MAX_RESULTS)
    futures = [pool.submit(_check, client, u, deadline) for u in urls]
    wait(futures, timeout=seconds)
    pool.shutdown(wait=False, cancel_futures=True)
    found = [
        f.result()
        if f.done()
        else Found(u, error="no answer in the time a tool call may take.")
        for u, f in zip(urls, futures)
    ]
    unique = list(
        {f.url: f for f in found}.values()
    )  # candidates that redirect to one feed
    return sorted(unique, key=lambda f: f.show is None)


def _apple(
    client: httpx.Client, url: str, deadline: float | None, **params
) -> list[str]:
    data, _ = read(client, url, deadline, media="podcast", **params)
    try:
        results = json.loads(data).get("results", [])
    except ValueError:
        raise FeedError(
            "Apple's podcast directory sent something that isn't JSON."
        ) from None
    return [r["feedUrl"] for r in results if r.get("feedUrl")]


class _FeedLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        rel = a.get("rel", "").lower().split()
        if (
            tag == "link"
            and "alternate" in rel
            and a.get("type", "").lower() == "application/rss+xml"
            and a.get("href")
        ):
            self.hrefs.append(a["href"])


def feed_links(html: str, base: str) -> list[str]:
    parser = _FeedLinks()
    parser.feed(html)
    return list(dict.fromkeys(urljoin(base, h) for h in parser.hrefs))


def find(
    client: httpx.Client, query: str, deadline: float | None = None
) -> tuple[list[Found], str]:
    """Candidate feeds, checked; and what to tell the model if there are none.

    `deadline` (time.monotonic()) bounds the whole search; feeds still loading then are
    reported as too slow."""
    query = clean_url(query)
    parts = urlsplit(query)
    if parts.scheme not in ("http", "https"):
        urls = _apple(
            client,
            APPLE_SEARCH,
            deadline,
            entity="podcast",
            term=query,
            limit=MAX_RESULTS,
        )
        return _check_all(client, urls, deadline), (
            f"Apple's podcast directory has nothing for '{query}'. Try another spelling, or the show's website."
        )
    if (parts.hostname or "").endswith("podcasts.apple.com") and (
        m := APPLE_ID_RE.search(parts.path)
    ):
        urls = _apple(client, APPLE_LOOKUP, deadline, id=m.group(1))
        return _check_all(
            client, urls, deadline
        ), "Apple lists that show without a public feed; it may be subscriber-only."

    data, resp = read(client, query, deadline)
    try:
        show, moved = parse(data)
    except FeedError:
        links = feed_links(data.decode("utf-8", "replace"), str(resp.url))[:MAX_RESULTS]
        return _check_all(client, links, deadline), (
            "That URL isn't a feed and doesn't link to one. Streaming-app pages (Spotify, Amazon Music, "
            "Audible, iHeart) never do: search by the show's name instead, and if nothing turns up, "
            "the show may be exclusive to that app, with no public feed."
        )
    return [
        _found(*follow_move(client, query, show, permanent_url(resp), moved, deadline))
    ], ""
