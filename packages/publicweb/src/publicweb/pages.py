"""Reading a web page's main text (a client that looks like a browser, a capped download
and trafilatura's extraction) and searching our SearXNG. The article writer and deep
research read and search with it.

Config (environment):
  SEARXNG_URL  the SearXNG search endpoint (default SEARXNG, the host's loopback; a
               service container, which can't reach it, uses https://<PUBLIC_HOST>:8888/search)
"""

import os
import threading
import time
from collections.abc import Callable

import httpx
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
    has under `min_chars` of text, or doesn't arrive by `deadline` (time.monotonic()).
    `error` is the client's error type (browser_client's)."""
    try:
        with stream(client, url, max_bytes, error, deadline) as (resp, body):
            if "html" not in resp.headers.get("content-type", ""):
                return None
            html = b"".join(body).decode(resp.encoding or "utf-8", errors="replace")
    except (httpx.HTTPError, error):
        return None
    text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    return text[:max_chars] if len(text) >= min_chars else None


Search = Callable[[str], list[dict]]
# The host's SearXNG, on its loopback (a container goes through PUBLIC_HOST's route).
SEARXNG = "http://127.0.0.1:8888/search"


def searxng_url() -> str:
    """The SearXNG to search: SEARXNG_URL, or the host's own."""
    return os.environ.get("SEARXNG_URL") or SEARXNG


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
