"""Web access for the workers: page reads (SearXNG search is publicweb.pages'), and the
quote checks of the agents engine (research.recipe)."""

import threading
from collections.abc import Callable

import httpx
from publicweb import host_name
from publicweb.pages import Page, browser_client, read_html, read_page

from research.config import CHECK_CHARS, LIMITS, PAGE_CHARS
from research.sources import quote_in_text

Read = Callable[[str], str | None]
# check(url, quote) -> the page's title (or host) when the quote is on it, else None.
Check = Callable[[str, str], str | None]


class ReadError(RuntimeError):
    pass


def page_client() -> httpx.Client:
    """For make_reader: public hosts only, redirects included, with a browser's headers."""
    return browser_client(ReadError)


def once_per_url[T](fetch: Callable[[str], T | None]) -> Callable[[str], T | None]:
    """`fetch`, with each URL fetched once (a second caller waits for the first), at most
    LIMITS["fetch"] at a time; a fetch that raises counts as None."""
    slots = threading.BoundedSemaphore(LIMITS["fetch"])
    cache: dict[str, T | None] = {}
    locks: dict[str, threading.Lock] = {}
    guard = threading.Lock()

    def get(url: str) -> T | None:
        with guard:
            lock = locks.setdefault(url, threading.Lock())
        with lock:
            if url not in cache:
                with slots:
                    try:
                        cache[url] = fetch(url)
                    except Exception:  # noqa: BLE001 - an unreadable page is just skipped
                        cache[url] = None
            return cache[url]

    return get


def make_checker(
    client: httpx.Client | None = None,
    fetch: Callable[[str], Page | None] | None = None,
) -> Check:
    """check(url, quote): whether `quote` is on the page at `url`, in its main text or
    anywhere else on it (at most CHECK_CHARS of each), fetched once per run with `fetch` if
    given, else through `client` (page_client: public hosts only)."""
    if fetch is None:
        if client is None:
            raise TypeError("make_checker needs a client or a fetch function")
        fetch = lambda url: read_page(client, url, ReadError)
    get = once_per_url(fetch)

    def check(url: str, quote: str) -> str | None:
        page = get(url)
        if page is None:
            return None
        if quote_in_text(quote, page.main[:CHECK_CHARS]) or quote_in_text(
            quote, page.full[:CHECK_CHARS]
        ):
            return page.title or host_name(url)
        return None

    return check


def make_reader(
    client: httpx.Client | None = None,
    fetch: Callable[[str], str | None] | None = None,
    max_chars: int = PAGE_CHARS,
) -> Read:
    """read(url) -> the page's main text (HTML only), or None. Each URL is read once per run,
    with `fetch` if given, else through `client` (page_client)."""
    if fetch is None:
        if client is None:
            raise TypeError("make_reader needs a client or a fetch function")
        fetch = lambda url: read_html(client, url, max_chars, ReadError)

    def text(url: str) -> str | None:
        found = (fetch(url) or "").strip()
        return found[:max_chars] if found else None

    return once_per_url(text)
