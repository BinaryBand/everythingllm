"""Reading a web page's main text (a client that looks like a browser, a capped download
and trafilatura's extraction) and searching our SearXNG. The article writer and deep
research read and search with it; deep research also checks quotes against a page's whole
text (read_page), since trafilatura leaves out what it doesn't take for the article."""

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import lxml.html
import trafilatura

from publicweb import public_client, stream

# Plenty of sites refuse anything that doesn't look like a browser: some CDNs want an
# Accept-Language (learnmeabitcoin.com), others the Sec-Fetch headers too (investopedia.com).
# No "br" in Accept-Encoding: httpx can't decode Brotli without the brotli package.
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
MAX_BYTES = 3 * 1024 * 1024
MIN_CHARS = 400  # less than this is a front page, a paywall or an error


def browser_client(error: type[Exception], timeout: float = 15) -> httpx.Client:
    """A public_client (public hosts only, redirects included) that sends a browser's headers."""
    return public_client(error, timeout=httpx.Timeout(timeout), headers=BROWSER_HEADERS)


@dataclass
class Page:
    title: str  # the page's <title>, or ""
    main: str  # trafilatura's main text: the article, without menus, comments or tables
    full: str  # all of the page's text, scripts and styles aside


# Elements whose end separates text, so their words don't run together in Page.full.
BLOCKS = frozenset(
    {
        *("address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt"),
        *("figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6"),
        *("header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "summary"),
        *("table", "td", "th", "title", "tr", "ul"),
    }
)
# An XML declaration naming an encoding, which lxml refuses in a decoded page.
XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>")


def page_text(html: str) -> tuple[str, str]:
    """An HTML page's title and whole text (entities decoded, scripts and styles left out)."""
    try:
        doc = lxml.html.fromstring(XML_DECLARATION.sub("", html, count=1))
    except Exception:  # noqa: BLE001 - lxml's ParserError (an empty page), which ty can't see
        return "", ""
    for el in doc.xpath("//script|//style|//noscript|//template"):
        el.drop_tree()
    title = " ".join((doc.findtext(".//title") or "").split())
    for el in doc.iter():
        if el.tag in BLOCKS:
            el.tail = "\n" + (el.tail or "")
    return title, doc.text_content()


def fetch_html(
    client: httpx.Client,
    url: str,
    error: type[Exception],
    deadline: float | None = None,
    max_bytes: int = MAX_BYTES,
) -> str | None:
    """An HTML page as text, or None when it isn't HTML or doesn't arrive by `deadline`
    (time.monotonic()). `error` is the client's error type (browser_client's)."""
    try:
        with stream(client, url, max_bytes, error, deadline) as (resp, body):
            if "html" not in resp.headers.get("content-type", ""):
                return None
            return b"".join(body).decode(resp.encoding or "utf-8", errors="replace")
    except (httpx.HTTPError, error):
        return None


def main_text(html: str) -> str:
    return trafilatura.extract(html, include_comments=False, include_tables=False) or ""


def read_page(
    client: httpx.Client,
    url: str,
    error: type[Exception],
    deadline: float | None = None,
    max_bytes: int = MAX_BYTES,
) -> Page | None:
    """An HTML page's title, main text and whole text, or None (as fetch_html)."""
    html = fetch_html(client, url, error, deadline, max_bytes)
    if html is None:
        return None
    title, full = page_text(html)
    return Page(title, main_text(html), full)


def read_html(
    client: httpx.Client,
    url: str,
    max_chars: int,
    error: type[Exception],
    deadline: float | None = None,
    max_bytes: int = MAX_BYTES,
    min_chars: int = MIN_CHARS,
) -> str | None:
    """The main text of an HTML page, at most `max_chars` of it, or None when it isn't HTML,
    has under `min_chars` of text, or doesn't arrive by `deadline` (time.monotonic())."""
    html = fetch_html(client, url, error, deadline, max_bytes)
    text = main_text(html) if html else ""
    return text[:max_chars] if len(text) >= min_chars else None


Search = Callable[[str], list[dict]]
# The host's SearXNG, on its loopback (the container goes through tailscale serve).
SEARXNG = "http://127.0.0.1:8888/search"


class SearchError(RuntimeError):
    pass


class _Pace:
    """One search at a time, `gap` seconds apart from the start of one to the next."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last_start = 0.0


# One queue and gap per SearXNG for the whole runner, so two runs at once don't search
# at twice the rate.
_paces: dict[str, _Pace] = {}
_paces_lock = threading.Lock()


def searxng_client() -> httpx.Client:
    return httpx.Client(
        timeout=30,
        headers={
            "Accept": "application/json",
            "User-Agent": "everythingllm",
        },
    )


def make_search(
    url: str, client: httpx.Client, gap: float = 2.0, limit: int = 8
) -> Search:
    """search(query) -> [{title, url, snippet}], deduplicated, http(s) only, the first
    `limit`. Searches through one SearXNG go one at a time, `gap` seconds apart: the
    engines behind it block bursts."""
    if not url:
        raise SearchError("No SearXNG URL is set.")
    with _paces_lock:
        pace = _paces.setdefault(url, _Pace())

    def search(query: str) -> list[dict]:
        with pace.lock:
            wait = pace.last_start + gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            pace.last_start = time.monotonic()
            try:
                resp = client.get(url, params={"q": query, "format": "json"})
            except httpx.HTTPError as e:
                raise SearchError(
                    f"couldn't reach SearXNG: {e or type(e).__name__}"
                ) from None
        if resp.status_code != 200:
            raise SearchError(f'SearXNG answered {resp.status_code} for "{query}"')
        try:
            data = resp.json()
        except ValueError:
            raise SearchError(f"SearXNG's answer for \"{query}\" wasn't JSON") from None
        seen, results = set(), []
        for r in data.get("results") or []:
            link = (r or {}).get("url") or ""
            if link in seen or not link.startswith(("http://", "https://")):
                continue
            seen.add(link)
            results.append(
                {
                    "title": str(r.get("title") or link),
                    "url": link,
                    "snippet": str(r.get("content") or ""),
                }
            )
            if len(results) >= limit:
                break
        # No results because the engines behind SearXNG refused us is a failure, not
        # an empty answer: say which ones, so the user can tell.
        down = [
            f"{name} ({why})" for name, why in (data.get("unresponsive_engines") or [])
        ]
        if not results and down:
            raise SearchError(f"no results; engines unavailable: {', '.join(down)}")
        return results

    return search
