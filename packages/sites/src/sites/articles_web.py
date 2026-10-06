"""The link behind every Daily News headline, served by sites-runner on its own port.

`tailscale serve` maps https://<host>:8445/news/write/ (`article_writer` in the site's
zola.toml) to this server, so it shares the news site's origin. A request for
`<day>/<desk>/<n>`, the n-th story of the edition's desk-th section (e.g. `2026-10-03/2/1`),
redirects to the story's article on the news site once it's written. Until then it
starts writing it and answers a page that reloads itself every few seconds; there's no
script, so it works under any CSP.

It listens on 127.0.0.1:8448 (PORT; tailscale serve maps :8445/news/write to it) and
searches the host's SearXNG.

Config (environment, from host.env and sites-runner's unit):
  ARTICLES_HOST    the address to listen on (default 127.0.0.1). In a container, 0.0.0.0:
                   its port is published on the host's 127.0.0.1, and what comes through
                   arrives from the container's own address
  SEARXNG_URL      the SearXNG to search (default the host's; publicweb.pages)
  SITES_SOURCE, ZOLA, ANYTHINGLLM_STORAGE   as for sites-build, with host paths
  DEEPSEEK_API_KEY or else read from ANYTHINGLLM_ENV (default .env in ANYTHINGLLM_STORAGE,
                   from host.env), as is the model that writes (DEEPSEEK_MODEL_PREF)
"""

import html
import os
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import hostrpc
from llm import deepseek, settings
from publicweb.pages import searxng_url

from sites.articles import Newsroom, NotFound, Story, find_story, page_gatherer
from sites.build import Builder
from sites.store import SiteError, SiteStore

PORT = 8448
HOST = "127.0.0.1"  # unless ARTICLES_HOST says otherwise

REFRESH_SECONDS = 4


def page(site_url: str, title: str, body: str, refresh: bool = False) -> bytes:
    """A page in the news site's own style sheets, which share this origin."""
    meta = f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">' if refresh else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{meta}
<title>{html.escape(title)} · Daily News</title>
<link rel="stylesheet" href="{site_url}agent-site.css">
<link rel="stylesheet" href="{site_url}news.css">
</head>
<body>
<header class="site"><a class="brand" href="{site_url}">Daily News</a></header>
<main>
{body}
</main>
</body>
</html>
""".encode()


def story_body(story: Story, message: str) -> str:
    return (
        f'<article class="story"><p class="kicker">{html.escape(story.desk)}</p>'
        f"<h1>{html.escape(story.headline)}</h1><p>{message}</p></article>"
    )


class Handler(BaseHTTPRequestHandler):
    newsroom: Newsroom
    site_url: str  # e.g. /news/
    prefix: str  # where tailscale serve mounts this server, e.g. /news/write
    route: re.Pattern

    @classmethod
    def configure(cls, newsroom: Newsroom, site_url: str, prefix: str) -> None:
        cls.newsroom, cls.site_url, cls.prefix = newsroom, site_url, prefix.rstrip("/")
        # With or without the prefix, whether or not tailscale serve strips it.
        cls.route = re.compile(
            rf"(?:{re.escape(cls.prefix)})?/(\d{{4}}-\d{{2}}-\d{{2}})/(\d{{1,2}})/(\d{{1,2}})/?"
        )

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/health":
            return self.send(HTTPStatus.OK, b"ok", "text/plain")
        match = self.route.fullmatch(url.path)
        if not match:
            return self.not_found("There's no story at this address.")
        day, desk, n = match[1], int(match[2]), int(match[3])
        room = self.newsroom
        try:
            story = find_story(room.store, room.site, day, desk, n)
        except NotFound as e:
            return self.not_found(f"Not found: {html.escape(str(e))}")
        if done := room.published(story):
            return self.redirect(done)
        if url.query == "retry=1":
            room.retry(story)
            return self.redirect(f"{self.prefix}/{day}/{desk}/{n}")
        if failure := room.status(story):
            link = (
                f' Read <a href="{html.escape(story.url)}">the original</a>, or'
                if story.url.startswith(("http://", "https://"))
                else ""
            )
            body = story_body(
                story,
                f"Couldn't write this article: {html.escape(failure.error)}"
                f'{link} <a href="?retry=1">try again</a>.',
            )
            return self.send(
                HTTPStatus.BAD_GATEWAY, page(self.site_url, story.headline, body)
            )
        body = story_body(
            story,
            "Writing this article from the source reporting. "
            "It takes about a minute; this page opens it when it's ready.",
        )
        self.send(
            HTTPStatus.OK, page(self.site_url, story.headline, body, refresh=True)
        )

    def not_found(self, message: str) -> None:
        self.send(
            HTTPStatus.NOT_FOUND,
            page(self.site_url, "Not found", f"<h1>Not found</h1><p>{message}</p>"),
        )

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send(
        self, status: HTTPStatus, data: bytes, ctype: str = "text/html; charset=utf-8"
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        pass  # tailscale serve and Caddy see the requests; the writer logs what it does


def server() -> ThreadingHTTPServer:
    """The article writer's server, configured from the environment; SiteError if it can't
    be (no DeepSeek key, no news site). sites-runner runs it in a thread of its own."""
    builder = Builder.from_env()
    store = SiteStore(builder.source, builder.content, build=builder.build)
    site = "news"
    key, model = settings(str(hostrpc.storage() / ".env"))
    if not key:
        raise SiteError("no DEEPSEEK_API_KEY in the environment or AnythingLLM's .env")

    news = next((s for s in store.sites() if s.name == site), None)
    if news is None:
        raise SiteError(f"no site named {site} in {builder.source}")
    config = store.config(site)
    Handler.configure(
        Newsroom(
            store, site, deepseek(key, model), page_gatherer(searxng_url()), model
        ),
        urlsplit(news.url).path,
        config["extra"]["article_writer"],
    )
    return ThreadingHTTPServer(address(), Handler)


def address() -> tuple[str, int]:
    """Where the article writer listens: ARTICLES_HOST (default HOST) and PORT."""
    return os.environ.get("ARTICLES_HOST") or HOST, PORT
