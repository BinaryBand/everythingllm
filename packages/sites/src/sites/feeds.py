"""Collect a section's recent stories from news feeds, for the Daily News job (the
`headlines` tool, run by sites-runner; see sites.tools).

Web search gave the job undated results and sites' front pages, so editions had stale
stories, sources from the wrong country and links like https://www.nbcnews.com/. Feeds
give each story's own link and when it was published. RSS 2.0, RSS 1.0 (RDF) and Atom
are read; feeds are fetched in parallel and one that fails is skipped and reported.
"""

import html
import math
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from urllib.parse import unquote_plus, urlsplit, urlunsplit

import httpx
from publicweb import public_client, read
from publicweb.pages import USER_AGENT  # some news sites refuse anything else


@dataclass(frozen=True)
class Feed:
    name: str  # credited as the story's source
    url: str


# The feeds per section, all checked to load without a key. Earlier feeds win when two
# carry the same story. Radio Sweden and The Local post little at weekends, so Sweden
# also reads Swedish-language SVT and Ekot; the model translates.
FEEDS: dict[str, list[Feed]] = {
    "US": [
        Feed("NPR", "https://feeds.npr.org/1003/rss.xml"),
        Feed("PBS NewsHour", "https://www.pbs.org/newshour/feeds/rss/headlines"),
        Feed("CBS News", "https://www.cbsnews.com/latest/rss/us"),
        Feed("ABC News", "https://abcnews.com/abcnews/usheadlines"),
        Feed(
            "The New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/US.xml"
        ),
    ],
    "Sweden": [
        Feed("Radio Sweden", "https://api.sr.se/api/rss/program/2054"),
        Feed("The Local Sweden", "https://feeds.thelocal.com/rss/se"),
        Feed("SVT Nyheter", "https://www.svt.se/nyheter/inrikes/rss.xml"),
        Feed("Sveriges Radio Ekot", "https://api.sr.se/api/rss/program/83"),
    ],
    "World": [
        Feed("BBC News", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        Feed("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        Feed("The Guardian", "https://www.theguardian.com/world/rss"),
        Feed("NPR", "https://feeds.npr.org/1004/rss.xml"),
        Feed("DW", "https://rss.dw.com/rdf/rss-en-world"),
        Feed("France 24", "https://www.france24.com/en/rss"),
    ],
}
WINDOW = timedelta(hours=30)  # a day's news, whatever time the job runs
MAX_STORIES = 15
SUMMARY_CHARS = 280
SAME_TITLE = 0.9  # SequenceMatcher ratio above which two headlines are one story
MAX_FEED_BYTES = 5 * 1024 * 1024
FETCH_SECONDS = 30  # all feeds together, well inside the 60 s a tool call may take
# Query parameters feeds add to links to count clicks; the article is the same without them.
TRACKING = re.compile(r"utm_.*|at_.*|traffic_source|maca|cmp|ns_.*", re.IGNORECASE)


class FeedError(RuntimeError):
    """A feed that couldn't be read; the message says why."""


@dataclass(frozen=True)
class Story:
    title: str
    summary: str
    source: str
    url: str
    published: datetime  # UTC


def make_client() -> httpx.Client:
    # The feeds are fixed, but redirects aren't, and this runs next to AnythingLLM's API.
    return public_client(
        FeedError,
        timeout=httpx.Timeout(15, connect=10),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
        },
    )


def clean(text: str, limit: int = 0) -> str:
    """Plain text from a feed field that may hold HTML, cut at a word to `limit` chars."""
    text = " ".join(html.unescape(re.sub(r"<[^>]*>", " ", text)).split())
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.-–—") + "…"
    return text


def canonical(url: str) -> str:
    """The link without click-tracking parameters or a fragment. The parameters kept stay
    as they were, byte for byte: re-encoding them would change "?12345" or a Latin-1
    escape, and the link with it."""
    parts = urlsplit(url.strip())
    query = [
        piece
        for piece in parts.query.split("&")
        if piece and not TRACKING.fullmatch(unquote_plus(piece.split("=", 1)[0]))
    ]
    return urlunsplit(parts._replace(query="&".join(query), fragment=""))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _date(text: str) -> datetime | None:
    """RSS 2.0's RFC 822 dates, or Atom's and Dublin Core's ISO 8601 ones, in UTC."""
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone(timezone.utc)
    except OverflowError:  # 9999-12-31T23:59-01:00 is past datetime.max in UTC
        return None


def _link(item: ET.Element) -> str:
    for el in item:
        if _local(el.tag) != "link":
            continue
        if el.get("href"):  # Atom
            if el.get("rel", "alternate") == "alternate":
                return el.get("href", "").strip()
        elif text := (el.text or "").strip():
            return text
    for el in item:  # an RSS guid is the link unless it says it isn't
        if _local(el.tag) == "guid" and el.get("isPermaLink", "true") != "false":
            return (el.text or "").strip()
    return ""


def parse(data: bytes, source: str) -> list[Story]:
    """The feed's stories that have a title, a web link and a date, newest first."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise FeedError(f"not valid XML: {e}") from None
    if _local(root.tag) not in ("rss", "feed", "RDF"):
        raise FeedError("not an RSS or Atom feed.")
    stories = []
    for item in root.iter():
        if _local(item.tag) not in ("item", "entry"):
            continue
        fields: dict[str, str] = {}
        for el in (
            item
        ):  # the first of each kept: RSS 2.0, Atom, Dublin Core and content: names
            fields.setdefault(_local(el.tag), "".join(el.itertext()))
        title = clean(fields.get("title", ""))
        url = _link(item)
        published = _date(
            fields.get("pubDate")
            or fields.get("published")
            or fields.get("date")
            or fields.get("updated")
            or ""
        )
        if (
            not title
            or not url.startswith(("http://", "https://"))
            or published is None
        ):
            continue
        summary = (
            fields.get("description")
            or fields.get("summary")
            or fields.get("encoded")
            or fields.get("content")
            or ""
        )
        stories.append(
            Story(
                title, clean(summary, SUMMARY_CHARS), source, canonical(url), published
            )
        )
    return sorted(stories, key=lambda s: s.published, reverse=True)


def fetch(client: httpx.Client, url: str, deadline: float) -> bytes:
    """The feed's body, capped at MAX_FEED_BYTES, read before `deadline` (time.monotonic())."""
    try:
        return read(client, url, MAX_FEED_BYTES, FeedError, deadline)[0]
    except httpx.HTTPStatusError as e:
        raise FeedError(f"HTTP {e.response.status_code}") from None
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise FeedError(str(e) or type(e).__name__) from None


def _key(title: str) -> str:
    return " ".join(re.findall(r"\w+", title.lower()))


def dedupe(stories: list[Story]) -> list[Story]:
    """The first of each story: same link, or a near-identical headline."""
    kept: list[Story] = []
    urls: set[str] = set()
    keys: list[str] = []
    for s in stories:
        key = _key(s.title)
        if s.url in urls or any(
            m.real_quick_ratio() >= SAME_TITLE
            and m.quick_ratio() >= SAME_TITLE
            and m.ratio() >= SAME_TITLE
            for m in (SequenceMatcher(None, key, k) for k in keys)
        ):
            continue
        kept.append(s)
        urls.add(s.url)
        keys.append(key)
    return kept


def pick(per_feed: list[list[Story]], limit: int = MAX_STORIES) -> list[Story]:
    """Up to `limit` stories, newest first, giving each feed a fair share before the
    busiest feeds fill what's left; per_feed lists are newest first."""
    share = math.ceil(limit / max(1, len(per_feed)))
    picked = [s for stories in per_feed for s in stories[:share]]
    rest = sorted(
        (s for stories in per_feed for s in stories[share:]),
        key=lambda s: s.published,
        reverse=True,
    )
    picked += rest[: max(0, limit - len(picked))]
    return sorted(picked, key=lambda s: s.published, reverse=True)[:limit]


def section_name(section: str) -> str:
    """The FEEDS key for `section`, ignoring case, or FeedError naming the choices."""
    for name in FEEDS:
        if name.lower() == section.strip().lower():
            return name
    raise FeedError(f"unknown section '{section}'; use one of: {', '.join(FEEDS)}.")


def headlines(
    client: httpx.Client,
    section: str,
    now: datetime | None = None,
    seconds: float = FETCH_SECONDS,
) -> tuple[list[Story], list[str]]:
    """The section's stories from the last WINDOW, and a note for each feed that failed."""
    feeds = FEEDS[section_name(section)]
    now = now or datetime.now(timezone.utc)
    deadline = time.monotonic() + seconds
    pool = ThreadPoolExecutor(len(feeds))
    futures = [
        pool.submit(lambda f: parse(fetch(client, f.url, deadline), f.name), f)
        for f in feeds
    ]
    wait(futures, timeout=seconds)
    # Don't wait for slow feeds: their threads finish (or time out) on their own.
    pool.shutdown(wait=False, cancel_futures=True)
    per_feed: list[list[Story]] = []
    failed = []
    for feed, future in zip(feeds, futures):
        if not future.done():
            failed.append(f"{feed.name} (no answer in {seconds:g} s)")
            continue
        try:
            stories = future.result()
        except Exception as e:  # noqa: BLE001 - one feed's failure (a bad date, an odd
            # encoding) is a note, never the section's
            failed.append(f"{feed.name} ({e})")
            continue
        # A little slack for clocks; a date further ahead is a feed's mistake.
        per_feed.append(
            [
                s
                for s in stories
                if now - WINDOW <= s.published <= now + timedelta(hours=1)
            ]
        )
    # In feed order, so a story both carry is credited to the earlier feed.
    kept = {id(s) for s in dedupe([s for stories in per_feed for s in stories])}
    return pick([[s for s in stories if id(s) in kept] for stories in per_feed]), failed
