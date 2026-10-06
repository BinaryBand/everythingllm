import json
import threading
import time
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
from sites import articles
from sites import articles_web as web
from sites.articles import (
    Failure,
    Newsroom,
    NotFound,
    Page,
    SourceError,
    WriteError,
    compose,
    find_story,
)
from sites.store import SiteStore

EDITION = {
    "sections": [
        {
            "name": "US",
            "stories": [
                {
                    "headline": "Senate passes budget",
                    "summary": "It passed.",
                    "source": "AP News",
                    "url": "https://apnews.com/",
                },
            ],
        },
        {
            "name": "Sweden",
            "stories": [
                {"headline": "Riksdag votes", "summary": "A vote.", "source": "SVT"},
                {
                    "headline": "Krona rises",
                    "summary": "Up.",
                    "source": "DN",
                    "url": "https://dn.se/a",
                },
            ],
        },
    ]
}
PAGES = [
    Page("https://apnews.com/", "AP News", "AP", "front page"),
    Page(
        "https://example.com/budget",
        "example.com",
        "Budget passes",
        "The Senate passed it.",
    ),
]


@pytest.fixture
def store(tmp_path):
    site = tmp_path / "src" / "news"
    for section in ("editions", "articles"):
        (site / "content" / section).mkdir(parents=True)
        (site / "content" / section / "_index.md").write_text("+++\n+++\n")
    (site / "zola.toml").write_text(
        'base_url = "https://pages/news"\ntitle = "Daily News"\n'
    )
    (tmp_path / "content").mkdir()
    s = SiteStore(tmp_path / "src", tmp_path / "content")
    s.write("news", "editions", "2026-10-03", "Daily News", "2026-10-03", EDITION)
    return s


def chat_replying(*replies):
    calls = []

    def chat(messages):
        calls.append(messages)
        return replies[len(calls) - 1]

    chat.calls = calls  # ty: ignore[unresolved-attribute]
    return chat


ARTICLE = json.dumps(
    {
        "paragraphs": [
            "The Senate passed the budget.",
            "  ",
            "It now goes to the House.",
        ],
        "sources": [2],
    }
)


def test_find_story_by_desk_and_place(store):
    story = find_story(store, "news", "2026-10-03", 2, 2)
    assert (story.headline, story.url, story.slug) == (
        "Krona rises",
        "https://dn.se/a",
        "sweden-2-2026-10-03",
    )
    assert find_story(store, "news", "2026-10-03", 2, 1).url == ""
    for day, desk, n in [
        ("2026-10-04", 1, 1),
        ("2026-10-03", 3, 1),
        ("2026-10-03", 1, 2),
        ("2026-10-03", 1, 0),
        ("2026-10-03", 0, 1),
    ]:
        with pytest.raises(NotFound):
            find_story(store, "news", day, desk, n)


def test_compose_keeps_only_used_pages_and_repairs_bad_json(store):
    story = find_story(store, "news", "2026-10-03", 1, 1)
    chat = chat_replying("not json", ARTICLE)
    paragraphs, used = compose(chat, story, PAGES)
    assert paragraphs == ["The Senate passed the budget.", "It now goes to the House."]
    assert used == [PAGES[1]]
    assert "Headline: Senate passes budget" in chat.calls[0][1]["content"]
    assert "[2] example.com: Budget passes" in chat.calls[0][1]["content"]


def test_compose_refuses_when_no_page_reports_the_story(store):
    story = find_story(store, "news", "2026-10-03", 1, 1)
    reply = json.dumps(
        {"paragraphs": [], "sources": [], "reason": "They are about sports."}
    )
    with pytest.raises(WriteError, match="They are about sports"):
        compose(chat_replying(reply), story, PAGES)


def test_write_publishes_an_article_that_matches_its_story(store):
    room = Newsroom(store, "news", chat_replying(ARTICLE), lambda story: PAGES, "m")
    story = find_story(store, "news", "2026-10-03", 1, 1)
    assert room.published(story) is None
    url = room.write(story)
    assert url == "https://pages/news/articles/us-1-2026-10-03/"
    assert room.published(story) == url
    entry, extra, body = store.get("news", "articles", "us-1-2026-10-03")
    assert entry.title == "Senate passes budget" and entry.date == "2026-10-03"
    assert extra["sources"] == [
        {"name": "example.com", "url": "https://example.com/budget"}
    ]
    assert extra["edition"] == "2026-10-03" and extra["desk"] == "US"
    assert body.strip() == "The Senate passed the budget.\n\nIt now goes to the House."

    # The edition's story changed, so the article no longer counts as written.
    changed = json.loads(json.dumps(EDITION))
    changed["sections"][0]["stories"][0]["headline"] = "Senate rejects budget"
    store.write(
        "news",
        "editions",
        "2026-10-03",
        "Daily News",
        "2026-10-03",
        changed,
        overwrite=True,
    )
    assert room.published(find_story(store, "news", "2026-10-03", 1, 1)) is None


def test_failures_are_remembered_until_retried(store):
    room = Newsroom(store, "news", chat_replying(), lambda story: [], "m")
    story = find_story(store, "news", "2026-10-03", 1, 1)
    assert room.status(story) is None  # writing
    deadline = time.monotonic() + 5
    while (
        not isinstance(status := room.status(story), Failure)
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    assert isinstance(status, Failure) and "could be read" in status.error
    room.retry(story)
    assert room.status(story) is None


def test_client_refuses_local_hosts():
    with (
        articles.make_client() as client,
        pytest.raises(SourceError, match="private or local"),
    ):
        client.get("http://localhost:3001/api")


def test_gather_reads_the_link_then_search_results(monkeypatch):
    def search(q):
        return [
            {"url": u, "title": t}
            for u, t in (
                ("https://a/1", "A"),
                ("https://x/", "X"),
                ("https://b/2", "B"),
            )
        ]

    texts = {"https://x/": None, "https://a/1": "a" * 500, "https://b/2": "b" * 500}
    monkeypatch.setattr(articles, "read", lambda client, url, deadline: texts[url])
    pages = articles.gather(None, search, "Headline", "https://x/", "X News")  # ty: ignore[invalid-argument-type]
    assert [(p.url, p.name) for p in pages] == [
        ("https://a/1", "a"),
        ("https://b/2", "b"),
    ]


def test_gather_drops_pages_still_loading_at_the_deadline(monkeypatch):
    monkeypatch.setattr(articles, "GATHER_SECONDS", 0.2)

    def search(q):
        return [
            {"url": "https://a/1", "title": "A"},
            {"url": "https://slow/", "title": "S"},
        ]

    release = threading.Event()

    def read(client, url, deadline):
        if url == "https://slow/":
            release.wait(5)
        return url[8] * 500

    monkeypatch.setattr(articles, "read", read)
    started = time.monotonic()
    pages = articles.gather(None, search, "Headline", None, "")  # ty: ignore[invalid-argument-type]
    release.set()
    assert time.monotonic() - started < 2
    assert [p.url for p in pages] == ["https://a/1"]


@pytest.fixture
def server(store):
    gate = threading.Event()

    def gather(story):
        gate.wait(5)
        return PAGES

    room = Newsroom(store, "news", chat_replying(ARTICLE), gather, "m")
    handler = type("H", (web.Handler,), {})
    handler.configure(room, "/news/", "/news/write/")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", gate
    gate.set()
    httpd.shutdown()


def test_web_writes_on_first_visit_then_redirects(server):
    base, gate = server
    first = httpx.get(f"{base}/news/write/2026-10-03/1/1")
    assert first.status_code == 200
    assert 'http-equiv="refresh"' in first.text and "Senate passes budget" in first.text
    gate.set()
    deadline = time.monotonic() + 5
    while (
        r := httpx.get(f"{base}/2026-10-03/1/1")
    ).status_code == 200 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert r.status_code == 303
    assert r.headers["location"] == "https://pages/news/articles/us-1-2026-10-03/"


def test_web_unknown_stories_are_404(server):
    base, _ = server
    assert httpx.get(f"{base}/news/write/2026-10-03/1/9").status_code == 404
    assert httpx.get(f"{base}/news/write/../../etc/passwd").status_code == 404
    assert httpx.get(f"{base}/other/2026-10-03/1/1").status_code == 404
    assert httpx.get(f"{base}/health").text == "ok"


def test_web_retry_clears_a_failure_and_redirects_back(store):
    room = Newsroom(store, "news", chat_replying(), lambda story: [], "m")
    handler = type("H", (web.Handler,), {})
    handler.configure(room, "/news/", "/news/write")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_port}"
        deadline = time.monotonic() + 5
        while (
            r := httpx.get(f"{base}/2026-10-03/1/1")
        ).status_code == 200 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert r.status_code == 502 and "try again" in r.text
        r = httpx.get(f"{base}/2026-10-03/1/1?retry=1")
        assert (r.status_code, r.headers["location"]) == (
            303,
            "/news/write/2026-10-03/1/1",
        )
    finally:
        httpd.shutdown()


def test_web_answers_only_loopback_and_its_own_address():
    own = ("10.89.79.12", 8448)  # sites-runner's container

    def status(peer, local=own):
        class Seen(web.Handler):
            def setup(self):
                super().setup()
                self.client_address = peer
                if local:
                    self.connection = SimpleNamespace(getsockname=lambda: local)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Seen)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            return httpx.get(f"http://127.0.0.1:{httpd.server_port}/health").status_code
        finally:
            httpd.shutdown()

    # Through the published port, from the container's own address; and on the host.
    assert status(("10.89.79.12", 40000)) == 200
    assert status(("127.0.0.1", 40000), None) == 200
    # Another container on egress-net.
    assert status(("10.89.79.13", 40000)) == 403


def test_the_writer_listens_on_loopback_unless_told_otherwise(monkeypatch):
    monkeypatch.delenv("ARTICLES_HOST", raising=False)
    assert web.address() == ("127.0.0.1", web.PORT)
    monkeypatch.setenv("ARTICLES_HOST", "0.0.0.0")  # in a container
    assert web.address() == ("0.0.0.0", web.PORT)


def test_the_writer_searches_the_searxng_its_told(monkeypatch):
    from publicweb import pages

    monkeypatch.delenv("SEARXNG_URL", raising=False)
    assert pages.searxng_url() == pages.SEARXNG == "http://127.0.0.1:8888/search"
    monkeypatch.setenv("SEARXNG_URL", "https://host.example:8888/search")
    assert pages.searxng_url() == "https://host.example:8888/search"
