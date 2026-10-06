"""Web access for the workers: page reads (SearXNG search is publicweb.pages')."""

import threading
from collections.abc import Callable

import httpx
from publicweb.pages import browser_client, read_html

from research.config import LIMITS, PAGE_CHARS

Read = Callable[[str], str | None]


class ReadError(RuntimeError):
    pass


def page_client() -> httpx.Client:
    """For make_reader: public hosts only, redirects included, with a browser's headers."""
    return browser_client(ReadError)


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
    slots = threading.BoundedSemaphore(LIMITS["fetch"])
    cache: dict[str, str | None] = {}
    locks: dict[str, threading.Lock] = {}
    guard = threading.Lock()

    def read(url: str) -> str | None:
        with guard:
            lock = locks.setdefault(url, threading.Lock())
        with lock:
            if url not in cache:
                with slots:
                    try:
                        text = (fetch(url) or "").strip()
                    except Exception:  # noqa: BLE001 - an unreadable page is just skipped
                        text = ""
                cache[url] = text[:max_chars] if text else None
            return cache[url]

    return read
