import json
import re
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import httpx
import pytest
from llm import LLMError
from podcasts import library, tools
from podcasts.library import (
    DISK_FULL,
    MIN_FREE_BYTES,
    NO_EPISODES,
    Library,
    LibraryError,
    make_client,
    read,
)
from podcasts.rss import ITUNES, Episode, FeedError, parse
from podcasts.rules import RuleError, judge, local_day
from publicweb import public_client

FEED_URL = "https://pod.example/feed.xml"


def feed(*eps: tuple[str, str], extra: str = "", moved: str = "") -> bytes:
    """eps: (guid, pubDate day of October 2026); moved: an itunes:new-feed-url."""
    items = "".join(
        f"""<item><title>Ep {g} &amp; more</title><guid>{g}</guid>
        <pubDate>{day} Oct 2026 10:00:00 +0000</pubDate>
        <description>&lt;p&gt;notes {g}&lt;/p&gt;</description>
        <itunes:duration>12:34</itunes:duration>
        <enclosure url="https://cdn.example/{g}.mp3?x=1" type="audio/mpeg" length="0"/></item>"""
        for g, day in eps
    )
    return f"""<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="{ITUNES}"><channel>
<title>The Show</title><link>https://pod.example/</link><description>About</description>
{f"<itunes:new-feed-url>{moved}</itunes:new-feed-url>" if moved else ""}
<itunes:image href="https://pod.example/cover.jpg"/>
<item><title>No audio</title><guid>text</guid></item>
{items}{extra}</channel></rss>""".encode()


class Remote:
    """A fake internet: the feed plus one file per episode."""

    def __init__(self, body: bytes):
        self.feed = body
        self.gets: list[str] = []
        self.fail: set[str] = set()
        self.pages: dict[str, httpx.Response] = {}  # other URLs: moved feeds, redirects

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.gets.append(url)
        if url in self.pages:
            return self.pages[url]
        if url == FEED_URL:
            return httpx.Response(
                200, content=self.feed, headers={"content-type": "application/rss+xml"}
            )
        name = request.url.path.strip("/")
        if name in self.fail:
            return httpx.Response(
                200, content=b"<html>gone</html>", headers={"content-type": "text/html"}
            )
        return httpx.Response(
            200,
            content=b"ID3" + name.encode() * 100,
            headers={"content-type": "audio/mpeg"},
        )

    def client(self) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(self.handler), follow_redirects=True
        )


def subscribe(lib: Library, remote: Remote, **add) -> str:
    """Adds the feed `remote` serves and syncs it; the show's slug."""
    with remote.client() as client:
        slug = lib.add(client, FEED_URL, **add)[0]
        lib.sync(client)
    return slug


def served(lib: Library, slug: str) -> list[str]:
    """The names splice-web serves episodes of `slug` under."""
    folder = lib.audio.manifests / slug
    if not folder.is_dir():
        return []
    return sorted(
        f.name.removesuffix(".json")
        for f in folder.glob("*.json")
        if not f.name.startswith(".")
    )


def test_parse_orders_newest_first_and_skips_items_without_audio():
    show, moved = parse(
        feed(("a", "1"), ("c", "3"), ("b", "2"), moved="https://new.example/")
    )
    assert moved == "https://new.example/"
    assert show.title == "The Show"
    assert show.image == "https://pod.example/cover.jpg"
    assert [e.guid for e in show.episodes] == ["c", "b", "a"]
    assert show.episodes[0].published == "2026-10-03T10:00:00+00:00"
    assert show.episodes[0].title == "Ep c & more"


@pytest.mark.parametrize(
    "data", [b"not xml", b"<feed xmlns='http://www.w3.org/2005/Atom'/>"]
)
def test_parse_rejects_non_rss(data):
    with pytest.raises(FeedError):
        parse(data)


def test_add_and_sync_downloads_newest_and_writes_private_feed(lib, tmp_path):
    # The announced move goes nowhere useful, so it's ignored but must not reach our feed.
    remote = Remote(
        feed(("a", "1"), ("b", "2"), ("c", "3"), moved="https://elsewhere.example/feed")
    )
    with remote.client() as client:
        slug, _show, new = lib.add(client, FEED_URL, keep=2)
        assert (slug, new) == ("the-show", True)
        assert lib.sync(client)

    folder = tmp_path / "site" / "the-show"
    assert sorted(p.name for p in folder.iterdir()) == ["feed.xml"]
    assert served(lib, "the-show") == [
        "2026-10-02-ep-b-more-e9d71f.mp3",
        "2026-10-03-ep-c-more-84a516.mp3",
    ]
    # The originals are kept by their hash, as downloaded.
    originals = {e.audio for e in lib.record("the-show")["show"].episodes}
    assert {p.name for p in (tmp_path / "state" / "audio").iterdir()} == originals
    assert all(re.fullmatch(r"[0-9a-f]{64}\.mp3", a) for a in originals)

    root = ET.fromstring((folder / "feed.xml").read_bytes())
    ch = root.find("channel")
    assert ch is not None
    assert ch.find(f"{{{ITUNES}}}new-feed-url") is None
    encs = [e.attrib for e in ch.findall("item/enclosure")]
    assert (
        encs[0]["url"]
        == "https://host.ts.net:8445/podcasts/the-show/2026-10-03-ep-c-more-84a516.mp3"
    )
    assert encs[0]["type"] == "audio/mpeg"
    assert (
        int(encs[0]["length"])
        == lib.audio.manifest("the-show", "2026-10-03-ep-c-more-84a516.mp3").size
    )
    assert [i.findtext("guid") for i in ch.findall("item")] == ["c", "b"]
    assert "the-show/feed.xml" in (tmp_path / "site" / "index.html").read_text()
    assert lib.record("the-show")["error"] == ""
    assert lib.feeds()["the-show"]["url"] == FEED_URL


NEW_URL = "https://new.example/feed.xml"


def rss(body: bytes) -> httpx.Response:
    return httpx.Response(
        200, content=body, headers={"content-type": "application/rss+xml"}
    )


@pytest.mark.parametrize(
    "status,moves", [(301, True), (308, True), (302, False), (307, False)]
)
def test_sync_saves_url_after_permanent_redirect(lib, status, moves):
    remote = Remote(feed(("a", "1")))
    with remote.client() as client:
        lib.add(client, FEED_URL)
        remote.pages[FEED_URL] = httpx.Response(status, headers={"location": NEW_URL})
        remote.pages[NEW_URL] = rss(feed(("a", "1"), ("b", "2")))
        lib.sync(client)
    assert lib.feeds()["the-show"]["url"] == (NEW_URL if moves else FEED_URL)
    assert [e.guid for e in lib.record("the-show")["show"].episodes] == ["b", "a"]


def test_sync_follows_announced_move_once(lib):
    remote = Remote(feed(("a", "1")))
    with remote.client() as client:
        lib.add(client, FEED_URL)
        remote.feed = feed(("a", "1"), moved=NEW_URL)
        # The new feed points back; that hop waits for the next sync, so no loop.
        remote.pages[NEW_URL] = rss(feed(("a", "1"), ("b", "2"), moved=FEED_URL))
        lib.sync(client)
        assert lib.feeds()["the-show"]["url"] == NEW_URL
        assert [e.guid for e in lib.record("the-show")["show"].episodes] == ["b", "a"]
        remote.gets.clear()
        lib.sync(client)
    assert remote.gets[:2] == [NEW_URL, FEED_URL]
    assert lib.feeds()["the-show"]["url"] == FEED_URL


@pytest.mark.parametrize(
    "page",
    [
        httpx.Response(404),
        rss(feed()),  # no episodes
        rss(b"not xml"),
    ],
)
def test_announced_move_ignored_unless_new_feed_works(lib, page):
    remote = Remote(feed(("a", "1"), moved=NEW_URL))
    remote.pages[NEW_URL] = page
    slug = subscribe(lib, remote)
    assert lib.feeds()[slug]["url"] == FEED_URL
    assert [e.guid for e in lib.record(slug)["show"].episodes] == ["a"]
    assert lib.record(slug)["error"] == ""


def test_announced_move_to_same_feed_is_not_fetched(lib):
    # Feeds often keep announcing their own address, give or take the scheme or a slash.
    remote = Remote(feed(("a", "1"), moved="http://pod.example/feed.xml/"))
    with remote.client() as client:
        lib.add(client, FEED_URL)
        remote.gets.clear()
        lib.sync(client)
    assert (
        remote.gets[0] == FEED_URL and "http://pod.example/feed.xml/" not in remote.gets
    )


def test_add_stores_the_moved_url(lib):
    remote = Remote(feed(("a", "1"), moved=NEW_URL))
    remote.pages[NEW_URL] = rss(feed(("a", "1")))
    with remote.client() as client:
        slug, _, _new = lib.add(client, FEED_URL)
        assert lib.feeds()[slug]["url"] == NEW_URL
        # The old URL still finds the same subscription.
        assert lib.add(client, FEED_URL, keep=3)[:3:2] == (slug, False)


def test_record_ignores_fields_dropped_since(lib, tmp_path):
    subscribe(lib, Remote(feed(("a", "1"))))
    file = tmp_path / "state" / "shows" / "the-show.json"
    d = json.loads(file.read_text())
    d["show"]["new_feed_url"] = "https://old.example/"
    d["show"]["episodes"][0]["gone"] = 1
    file.write_text(json.dumps(d))
    assert [e.guid for e in lib.record("the-show")["show"].episodes] == ["a"]


def test_sync_skips_downloaded_and_prunes_old(lib, tmp_path):
    remote = Remote(feed(("a", "1"), ("b", "2")))
    subscribe(lib, remote, keep=2)
    with remote.client() as client:
        remote.feed = feed(("a", "1"), ("b", "2"), ("c", "3"))
        remote.gets.clear()
        lib.sync(client)
    assert remote.gets == [FEED_URL, "https://cdn.example/c.mp3?x=1"]
    eps = lib.record("the-show")["show"].episodes
    assert [e.guid for e in eps] == ["c", "b"]
    assert not any("ep-a" in p.name for p in (tmp_path / "site" / "the-show").iterdir())


def judging(skip: str = "", fail: bool = False):
    """A fake model that skips episodes whose titles contain `skip`; with `fail`, it can't be
    reached. Its calls are the episode titles it was asked about."""
    calls = []

    def chat(messages):
        lines = re.findall(
            r"^\[(\d+)\] .* · (.*)$", messages[1]["content"], re.MULTILINE
        )
        calls.append([title for _, title in lines])
        if fail:
            raise LLMError("couldn't reach DeepSeek: down")
        return json.dumps(
            {
                "episodes": [
                    {
                        "n": int(n),
                        "keep": not (skip and skip in title),
                        "why": "the rules say so",
                    }
                    for n, title in lines
                ]
            }
        )

    chat.calls = calls  # ty: ignore[unresolved-attribute]
    return chat


def test_rules_leave_episodes_out_and_they_dont_count_toward_keep(lib, tmp_path):
    lib.chat = judging(skip="trailer")
    remote = Remote(feed(("a", "1"), ("trailer", "2"), ("b", "3"), ("c", "4")))
    subscribe(lib, remote, keep=2)
    with remote.client() as client:
        assert [e.guid for e in lib.record("the-show")["show"].episodes] == ["c", "b"]
        assert lib.chat.calls == []  # no rules, no model
        lib.add(client, FEED_URL, keep=3, rules="  Skip the trailers. ")
        assert lib.feeds()["the-show"]["rules"] == "Skip the trailers."
        remote.gets.clear()
        lib.sync(client)
        assert remote.gets == [FEED_URL, "https://cdn.example/a.mp3?x=1"]
        assert [e.guid for e in lib.record("the-show")["show"].episodes] == [
            "c",
            "b",
            "a",
        ]
        assert lib.chat.calls == [
            ["Ep c & more", "Ep b & more", "Ep trailer & more", "Ep a & more"]
        ]
        lib.sync(client)
        assert len(lib.chat.calls) == 1  # each episode is judged once
        lib.add(client, FEED_URL, keep=3)  # leaving rules out keeps them
        assert lib.feeds()["the-show"]["rules"] == "Skip the trailers."
        lib.add(client, FEED_URL, keep=3, rules="Skip the trailers!")
        lib.sync(client)
        assert len(lib.chat.calls) == 2  # new rules, judged again
        lib.add(client, FEED_URL, keep=3, rules="")
        assert lib.feeds()["the-show"]["rules"] == ""
        with pytest.raises(LibraryError):
            lib.add(client, FEED_URL, rules="x" * 2001)


def test_rules_prune_episodes_already_downloaded(lib, tmp_path, monkeypatch):
    lib.chat = judging(skip="Ep b")
    remote = Remote(feed(("a", "1"), ("b", "2")))
    subscribe(lib, remote, keep=2)
    subscribe(lib, remote, keep=2, rules="No episode b.")
    assert [e.guid for e in lib.record("the-show")["show"].episodes] == ["a"]
    assert not any("ep-b" in p.name for p in (tmp_path / "site" / "the-show").iterdir())
    assert "rules: 'No episode b.'" in tools._settings(lib.feeds()["the-show"])
    monkeypatch.setattr(tools, "lib", lambda: lib)
    assert (
        "  skipped 2026-10-02 Ep b & more (the rules say so)"
        in tools.list_podcasts().splitlines()
    )


def test_rules_only_judge_as_far_back_as_keep_needs(lib, monkeypatch):
    monkeypatch.setattr(library, "BATCH", 2)
    lib.chat = judging(skip="Ep d")
    remote = Remote(feed(*((g, str(i)) for i, g in enumerate("abcdef", 1))))
    subscribe(lib, remote, keep=2, rules="No d.")
    assert [e.guid for e in lib.record("the-show")["show"].episodes] == ["f", "e"]
    assert lib.chat.calls == [["Ep f & more", "Ep e & more"]]


def test_unjudged_episodes_wait_and_downloads_stay(lib, tmp_path):
    lib.chat = judging()
    remote = Remote(feed(("a", "1"), ("b", "2")))
    subscribe(lib, remote, keep=2, rules="Keep everything.")
    with remote.client() as client:
        lib.chat = judging(fail=True)
        remote.feed = feed(("a", "1"), ("b", "2"), ("c", "3"))
        remote.gets.clear()
        lib.sync(client)
        assert remote.gets == [FEED_URL]  # c waits, b and a stay
        assert [e.guid for e in lib.record("the-show")["show"].episodes] == ["b", "a"]
        assert (
            lib.record("the-show")["error"]
            == "couldn't apply the rules: couldn't reach DeepSeek: down"
        )
        lib.add(
            client, FEED_URL, keep=2, rules="Keep it all."
        )  # new rules, none judged
        lib.sync(client)
        assert [e.guid for e in lib.record("the-show")["show"].episodes] == ["b", "a"]
        lib.chat = None
        lib.sync(client)
        assert lib.record("the-show")["error"] == library.NO_MODEL
        lib.chat = judging()
        lib.sync(client)
    assert [e.guid for e in lib.record("the-show")["show"].episodes] == ["c", "b"]
    assert lib.record("the-show")["error"] == ""


def test_an_unjudged_episode_holds_back_older_new_ones(lib):
    # The model answered for c but not b: a, older than b, isn't fetched just to be pruned.
    remote = Remote(feed(("a", "1"), ("b", "2"), ("c", "3")))
    with remote.client() as client:
        lib.add(client, FEED_URL, keep=2, rules="Anything.")
        (lib.state / "verdicts").mkdir()
        verdict = {"keep": True, "why": "", "title": "", "published": ""}
        (lib.state / "verdicts" / "the-show.json").write_text(
            json.dumps({"rules": "Anything.", "episodes": {"c": verdict, "a": verdict}})
        )
        lib.chat = judging(fail=True)
        lib.sync(client)
    assert [e.guid for e in lib.record("the-show")["show"].episodes] == ["c"]


def test_judge_shows_the_model_local_weekdays_and_plain_descriptions(monkeypatch):
    monkeypatch.delenv("PODCASTS_TZ", raising=False)
    ep = Episode(
        "g",
        "Jon on Canada",
        "u",
        published="2026-10-02T23:00:00+00:00",
        description="<p>Jon Stewart &amp; friends</p>",
        duration="3500",
    )
    chat = judging()
    sent = []
    judge(lambda m: sent.append(m) or chat(m), "Only Jon.", [ep])
    assert "Only Jon." in sent[0][0]["content"]
    assert (
        sent[0][1]["content"]
        == "[1] Saturday 2026-10-03 · 58:20 · Jon on Canada\n    Jon Stewart & friends"
    )


@pytest.mark.parametrize(
    "answer",
    [
        "Keep them all.",
        '{"episodes": [{"n": 1, "keep": true}]}',
        '{"episodes": [{"n": 1, "keep": "yes"}, {"n": 2, "keep": true}]}',
    ],
)
def test_judge_refuses_answers_it_cant_use(answer):
    eps = [Episode("a", "A", "u"), Episode("b", "B", "u")]
    with pytest.raises(RuleError):
        judge(lambda m: answer, "Anything.", eps)


def test_local_day_is_in_the_users_time_zone(monkeypatch):
    monkeypatch.delenv("PODCASTS_TZ", raising=False)
    assert (
        local_day("2026-10-02T23:00:00+00:00") == "Saturday 2026-10-03"
    )  # 1 am Saturday in Stockholm
    monkeypatch.setenv("PODCASTS_TZ", "America/Phoenix")
    assert local_day("2026-10-03T02:00:00+00:00") == "Friday 2026-10-02"


def test_keep_all_downloads_the_catalog_a_daily_limit_at_a_time(
    lib, tmp_path, monkeypatch
):
    monkeypatch.setattr(library, "DAILY_DOWNLOADS", 2)
    remote = Remote(feed(*((g, str(i)) for i, g in enumerate("abcde", 1))))
    subscribe(lib, remote, keep="all")
    with remote.client() as client:
        assert [e.guid for e in lib.record("the-show")["show"].episodes] == [
            "e",
            "d",
        ]  # newest first
        assert (
            lib.record("the-show")["error"]
            == "downloaded 2 today, the daily limit; 3 more come in the next days"
        )
        remote.gets.clear()
        lib.sync(client)
        assert remote.gets == [FEED_URL]  # still today
        counts = tmp_path / "state" / "downloads.json"
        counts.write_text(json.dumps({"the-show": {"day": "2026-01-01", "count": 2}}))
        lib.sync(client)
        assert [e.guid for e in lib.record("the-show")["show"].episodes] == [
            "e",
            "d",
            "c",
            "b",
        ]
        counts.write_text(json.dumps({"the-show": {"day": "2026-01-02", "count": 2}}))
        lib.sync(client)
    assert [e.guid for e in lib.record("the-show")["show"].episodes] == [
        "e",
        "d",
        "c",
        "b",
        "a",
    ]
    assert lib.record("the-show")["error"] == ""
    assert "keeping every episode" in tools._keeping(lib.feeds()["the-show"]["keep"])
    with remote.client() as client, pytest.raises(LibraryError):
        lib.add(client, FEED_URL, keep="everything")


def test_failed_download_is_reported_and_left_out_of_feed(lib, tmp_path):
    remote = Remote(feed(("a", "1"), ("b", "2")))
    remote.fail.add("b.mp3")
    subscribe(lib, remote)
    rec = lib.record("the-show")
    assert [e.guid for e in rec["show"].episodes] == ["a"]
    assert "Ep b & more: the server sent text/html" in rec["error"]
    assert not list((tmp_path / "site" / "the-show").glob("*.part"))


def test_feed_error_keeps_existing_downloads(lib):
    remote = Remote(feed(("a", "1")))
    subscribe(lib, remote)
    with remote.client() as client:
        remote.feed = b"oops"
        lib.sync(client)
    rec = lib.record("the-show")
    assert "not valid XML" in rec["error"]
    assert [e.guid for e in rec["show"].episodes] == ["a"]


def test_add_again_changes_keep_and_validates(lib):
    remote = Remote(feed(("a", "1")))
    with remote.client() as client:
        lib.add(client, FEED_URL, keep=3)
        slug, _, new = lib.add(client, FEED_URL, keep=7)
        assert (slug, new) == ("the-show", False)
        assert lib.feeds()["the-show"]["keep"] == 7
        with pytest.raises(LibraryError):
            lib.add(client, FEED_URL, keep=0)
        with pytest.raises(LibraryError):
            lib.add(client, "file:///etc/passwd")


def test_remove_deletes_downloads(lib, tmp_path):
    subscribe(lib, Remote(feed(("a", "1"))))
    lib.remove("the-show")
    assert lib.feeds() == {}
    assert not (tmp_path / "site" / "the-show").exists()
    with pytest.raises(LibraryError):
        lib.remove("the-show")


def test_remove_refused_while_sync_runs(lib):
    remote = Remote(feed(("a", "1")))
    with remote.client() as client:
        lib.add(client, FEED_URL)
    with lib._lock("sync.lock"):
        assert lib.sync_running()
        with pytest.raises(LibraryError, match="sync is running"):
            lib.remove("the-show")


def test_private_hosts_refused():
    with make_client() as client, pytest.raises(FeedError, match="private or local"):
        client.get("http://10.0.0.5/feed.xml")


OTHER_URL = "https://other.example/feed.xml"


def bad_item(guid: str, host: str) -> str:
    return f"""<item><title>Ep {guid}</title><guid>{guid}</guid><pubDate>9 Oct 2026 10:00:00 +0000</pubDate>
    <enclosure url="https://{host}/{guid}.mp3" type="audio/mpeg"/></item>"""


def test_empty_feed_keeps_downloads(lib, tmp_path):
    remote = Remote(feed(("a", "1"), ("b", "2")))
    subscribe(lib, remote)
    with remote.client() as client:
        folder = tmp_path / "site" / "the-show"
        files = sorted(p.name for p in folder.iterdir())
        remote.feed = feed()
        lib.sync(client)
    assert sorted(p.name for p in folder.iterdir()) == files
    rec = lib.record("the-show")
    assert rec["error"] == NO_EPISODES
    assert [e.guid for e in rec["show"].episodes] == ["b", "a"]


def test_malformed_enclosure_host_fails_only_that_episode(lib, tmp_path, monkeypatch):
    # The real resolver rejects "cdn..example" with UnicodeError before any DNS; every
    # other host gets a public address, so the real public_client checks run.
    real = socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        if ".." in host:
            return real(host, port, *args, **kwargs)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    remote = Remote(feed(("a", "1"), extra=bad_item("bad", "cdn..example")))
    remote.pages[OTHER_URL] = rss(feed(("z", "5")))
    with public_client(
        FeedError, transport=httpx.MockTransport(remote.handler)
    ) as client:
        lib.add(client, FEED_URL)
        lib.add(client, OTHER_URL, slug="other")
        assert lib.sync(client)
    rec = lib.record("the-show")
    assert [e.guid for e in rec["show"].episodes] == ["a"]
    assert "Ep bad: cdn..example isn't a valid host name" in rec["error"]
    assert [e.guid for e in lib.record("other")["show"].episodes] == ["z"]
    assert lib.record("other")["error"] == ""


def test_crashing_feed_does_not_stop_the_next(lib, monkeypatch):
    remote = Remote(feed(("a", "1")))
    remote.pages[OTHER_URL] = rss(feed(("z", "5")))
    download = lib._download

    def buggy(client, slug, ep):
        if slug == "the-show":
            raise KeyError("oops")
        return download(client, slug, ep)

    monkeypatch.setattr(lib, "_download", buggy)
    with remote.client() as client:
        lib.add(client, FEED_URL)
        lib.add(client, OTHER_URL, slug="other")
        assert lib.sync(client)
    assert lib.record("the-show")["error"] == "sync failed: KeyError: 'oops'"
    assert [e.guid for e in lib.record("other")["show"].episodes] == ["z"]
    last = lib.last_sync()
    assert last["started"] and last["finished"] and last["error"] == ""


def test_crash_is_recorded_and_listed(lib, monkeypatch):
    subscribe(lib, Remote(feed(("a", "1"))))
    lib.sync_crashed(
        "Traceback (most recent call last):\n  File x\nOSError: [Errno 30] Read-only file system\n"
    )
    assert lib.last_sync()["error"].endswith(
        "OSError: [Errno 30] Read-only file system"
    )
    monkeypatch.setattr(tools, "lib", lambda: lib)
    out = tools.list_podcasts()
    assert "crashed: OSError: [Errno 30] Read-only file system" in out


def test_disk_floor_pauses_downloads(lib, free_disk):
    remote = Remote(feed(("a", "1")))
    remote.pages[OTHER_URL] = rss(feed(("z", "5")))
    with remote.client() as client:
        lib.add(client, FEED_URL)
        lib.add(client, OTHER_URL, slug="other")
        free_disk(MIN_FREE_BYTES - 1)
        lib.sync(client)
        assert not [g for g in remote.gets if "cdn.example" in g]
        assert lib.record("the-show")["error"] == DISK_FULL
        assert lib.record("other")["error"] == DISK_FULL
        free_disk(MIN_FREE_BYTES)
        lib.sync(client)
    assert [e.guid for e in lib.record("the-show")["show"].episodes] == ["a"]
    assert lib.record("other")["error"] == ""


def slow(chunks: int, pause: float):
    for _ in range(chunks):
        time.sleep(pause)
        yield b"x" * 10


def test_read_gives_up_at_the_deadline():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=slow(50, 0.02))
    )
    with httpx.Client(transport=transport) as client:
        started = time.monotonic()
        with pytest.raises(FeedError, match="gave up"):
            read(client, FEED_URL, time.monotonic() + 0.1)
        assert time.monotonic() - started < 0.5
        with pytest.raises(FeedError, match="gave up"):
            read(client, FEED_URL, time.monotonic() - 1)


def test_add_passes_its_deadline_on(lib):
    remote = Remote(feed(("a", "1")))
    with remote.client() as client, pytest.raises(FeedError, match="gave up"):
        lib.add(client, FEED_URL, deadline=time.monotonic() - 1)
    assert remote.gets == []


class FakeUnits:
    """Runs a unit's sync as a child process: `script` (given the unit's instance), or the
    real podcasts-sync, as podcasts-sync@.service would."""

    def __init__(self, lib, script: str = ""):
        self.lib, self.script, self.procs, self.started = lib, script, {}, []

    def start(self, unit):
        instance = unit.removeprefix("podcasts-sync@").removesuffix(".service")
        cmd = ["-c", self.script] if self.script else ["-m", "podcasts.sync"]
        with open(self.lib.state / "sync.log", "ab") as log:
            self.procs[unit] = subprocess.Popen(
                [sys.executable, *cmd, instance], stdout=log, stderr=log
            )
        self.started.append(unit)

    def failed(self, unit):
        return self.procs[unit].poll() not in (None, 0)


def test_start_sync_starts_a_unit_for_the_feed_or_all(lib, monkeypatch):
    monkeypatch.setattr(library.time, "sleep", lambda s: None)
    lib.units = FakeUnits(lib, "import time; time.sleep(0.2)")
    assert lib.start_sync("the-show") and lib.start_sync()
    assert lib.units.started == [
        "podcasts-sync@the-show.service",
        "podcasts-sync@_all.service",
    ]
    for p in lib.units.procs.values():
        p.wait()


def test_start_sync_leaves_a_running_sync_be(lib):
    lib.units = FakeUnits(lib)
    with lib._lock("sync.lock"):
        assert lib.sync_running()
        assert not lib.start_sync("the-show")
    assert lib.units.started == []


def test_start_sync_reports_a_sync_that_dies_at_once(lib):
    lib.units = FakeUnits(
        lib, "import sys\nprint('ModuleNotFoundError: no podcasts')\nsys.exit(1)\n"
    )
    with pytest.raises(
        LibraryError, match="the sync failed to start: ModuleNotFoundError: no podcasts"
    ):
        lib.start_sync()
    assert not lib.sync_running()


def test_start_sync_runs_the_real_sync(lib, tmp_path, monkeypatch):
    monkeypatch.setenv("PODCASTS_DIR", str(tmp_path / "site"))
    monkeypatch.setenv("PODCASTS_STATE", str(tmp_path / "state"))
    lib.units = FakeUnits(lib)
    assert lib.start_sync()  # podcasts-sync _all: every feed
    for _ in range(150):
        if (lib.last_sync() or {}).get("finished"):
            break
        time.sleep(0.1)
    assert lib.last_sync()["error"] == ""
    assert "another sync" not in (tmp_path / "state" / "sync.log").read_text()


def test_from_env_defaults_to_everythingllms_data(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_HOST", "box.ts.net")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    lib = Library.from_env()
    data = tmp_path / "home" / ".local" / "share" / "everythingllm"
    assert (lib.state, lib.site) == (data / "podcasts", data / "site" / "podcasts")
    assert lib.base_url == "https://box.ts.net:8445/podcasts"
    assert library.models_dir("whisper") == data / "models" / "whisper"


def test_splice_web_knows_every_type_we_download():
    from splice.web import TYPES

    assert {f".{ext}" for ext in library.EXT_TYPE} <= set(TYPES)


def test_the_mcp_server_forwards_to_the_runner(lib, monkeypatch):
    import asyncio
    import os
    from pathlib import Path

    import hostrpc
    from mcp.server.mcpserver.exceptions import ToolError
    from podcasts import server

    monkeypatch.setattr(tools, "lib", lambda: lib)
    sock = Path("/tmp") / f"podcasts-test-{os.getpid()}.sock"  # AF_UNIX paths are short
    monkeypatch.setenv("PODCASTS_SOCKET", str(sock))

    async def go():
        task = asyncio.create_task(hostrpc.serve(tools.runner, sock))
        for _ in range(100):
            if sock.exists():
                break
            await asyncio.sleep(0.01)
        try:
            listed = {t.name for t in await server.mcp.list_tools()}
            assert listed == {f.__name__ for f in tools.OPS}
            assert (await server.list_podcasts()).startswith("No podcasts yet.")
            assert await server.refresh_podcasts() == "No podcasts to refresh."
            with pytest.raises(ToolError, match="no podcast named 'nope'"):
                await server.refresh_podcasts("nope")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        with pytest.raises(
            ToolError, match="podcasts runner isn't running on the host"
        ):
            await server.list_podcasts()

    asyncio.run(go())
