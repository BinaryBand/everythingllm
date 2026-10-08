"""The live cards (browser.live): a tab's screenshot under a strip, pushed as it changes;
the card's link goes to the take-over view while the browser runs."""

import asyncio
import io
import json

import pytest
from browser import live
from browser.runner import Runner
from browser_fakes import Clock, FakePodman, config, jpeg, scope
from chatimage import THEMES
from PIL import Image


def test_a_frame_is_the_strip_over_the_screenshot_dimmed_once_closed():
    shot = jpeg((250, 250, 250), (640, 400))  # scaled to the card's width
    for state in live.STATES:
        image = Image.open(
            io.BytesIO(
                live.picture(shot, "career", state, "T", "https://x/", "Clicked e1")
            )
        )
        assert image.format == "JPEG" and image.size == (live.WIDTH, live.STRIP + 800)
        middle = image.convert("RGB").getpixel((live.WIDTH // 2, live.STRIP + 400))
        assert isinstance(middle, tuple)
        assert (middle[0] > 200) == (state != "closed")
    blank = Image.open(io.BytesIO(live.picture(b"", "career", "working", "", "", "")))
    assert blank.size == (live.WIDTH, live.STRIP + 360)

    def near(a, b):  # a JPEG's colours are only close
        return all(abs(x - y) <= 6 for x, y in zip(a, b, strict=True))

    for theme, p in THEMES.items():
        themed = Image.open(
            io.BytesIO(live.picture(b"", "career", "user", "", "", "", theme))
        ).convert("RGB")
        assert near(themed.getpixel((live.WIDTH // 2, live.STRIP + 20)), p.panel)
        assert near(themed.getpixel((4, 20)), p.user)  # the strip's stripe
    assert (
        Image.open(
            io.BytesIO(live.picture(b"not a jpeg", "w", "idle", "", "", ""))
        ).size[0]
        == live.WIDTH
    )


async def get(port, path, method="GET"):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"{method} {path} HTTP/1.1\r\nHost: h\r\n\r\n".encode())
    await writer.drain()
    return reader, writer


async def first_frame(reader):
    head = await reader.readuntil(b"\r\n\r\n")
    part = await reader.readuntil(b"\r\n\r\n")
    length = int(part.split(b"Content-Length: ")[1].split(b"\r\n")[0])
    return head, part, await reader.readexactly(length)


@pytest.mark.xdist_group("timing")
def test_the_card_streams_the_tab_and_links_to_the_take_over_view(tmp_path):
    async def main():
        podman = FakePodman()
        r = Runner(config(tmp_path), podman=podman, now=Clock())
        server = await live.Live(r).serve(0)
        port = server.sockets[0].getsockname()[1]
        try:
            await r.op_open(scope(), "example.com")
            tab = r.threads[("career", "7")]
            for path in (
                f"/_live/browser/{tab.id}.jpg",
                f"/{tab.id}.jpg",
            ):  # a route that strips the prefix
                reader, writer = await get(port, path)
                head, part, frame = await first_frame(reader)
                assert b"multipart/x-mixed-replace" in head and b"image/jpeg" in part
                assert b"Access-Control" not in head  # its screenshots are the user's
                assert Image.open(io.BytesIO(frame)).format == "JPEG"
                assert tab.viewers == 1  # watching counts, which keeps the browser up
                writer.close()
                await asyncio.sleep(1.2)
                assert tab.viewers == 0
            reader, writer = await get(port, f"/_live/browser/{tab.id}")
            head = await reader.read()
            s = r.sessions["career"]
            assert (
                b"302 Found" in head
                and f"Location: {r.takeover(s, tab)}".encode() in head
            )
            await r.stop("career")
            reader, _ = await get(port, f"/_live/browser/{tab.id}")
            head, _, body = (await reader.read()).partition(b"\r\n\r\n")
            assert (
                b"200 OK" in head
                and f"Content-Security-Policy: {live.CSP}".encode() in head
            )
            assert b"tab is closed" in body
            reader, _ = await get(port, "/_live/browser/bw-0000000000000000.jpg")
            head, _, body = (await reader.read()).partition(b"\r\n\r\n")
            assert b"image/png" in head and Image.open(io.BytesIO(body)).format == "PNG"
            reader, _ = await get(port, "/_live/browser/bw-nothex.jpg")
            assert b"404" in await reader.read()
            reader, _ = await get(port, f"/_live/browser/{tab.id}.jpg", "POST")
            assert b"405" in await reader.read()
        finally:
            server.close()
            await podman.close()

    asyncio.run(main())


@pytest.mark.xdist_group("timing")
def test_a_closed_tab_shows_its_last_look_until_its_opened_again(tmp_path):
    async def main():
        podman = FakePodman()
        r = Runner(config(tmp_path), podman=podman, now=Clock())
        try:
            await r.op_open(scope(), "example.com")
            tab = r.threads[("career", "7")]
            frames = live.Live(r).frames(tab)
            await anext(frames)
            await r.op_close(scope())
            closed = Image.open(io.BytesIO(await anext(frames)))
            assert (
                closed.convert("L").getpixel((live.WIDTH // 2, live.STRIP + 300)) < 120  # ty: ignore[unsupported-operator]
            )  # dimmed
            waiting = asyncio.ensure_future(anext(frames))
            await asyncio.sleep(0.1)
            assert not waiting.done()  # a closed tab waits to be used again
            await r.op_open(scope(), "example.com")
            await asyncio.wait_for(waiting, 2)
            await frames.aclose()  # ty: ignore[unresolved-attribute] - a generator's
            assert tab.viewers == 0
        finally:
            await podman.close()

    asyncio.run(main())


def test_a_login_requests_card_shows_how_it_stands_and_links_to_its_form(tmp_path):
    async def main():
        podman = FakePodman()
        r = Runner(config(tmp_path), podman=podman, now=Clock())
        server = await live.Live(r).serve(0)
        port = server.sockets[0].getsockname()[1]
        try:
            await r.op_open(scope(), "https://linkedin.com/login")
            asked = await r.op_ask_login(scope())
            req = r.asked[asked["request"]]
            for path in (f"/_live/browser/login/{req.id}.png", f"/login/{req.id}.png"):
                reader, writer = await get(port, path)
                head, part, frame = await first_frame(reader)
                assert b"multipart/x-mixed-replace" in head and b"image/png" in part
                assert Image.open(io.BytesIO(frame)).format == "PNG"
                writer.close()
            # The stream sends the answer, then ends.
            reader, writer = await get(port, f"/_live/browser/login/{req.id}.png")
            await first_frame(reader)
            await r.fulfil(req, "linkedin.com", "alice", "pw", "", False)
            rest = await asyncio.wait_for(reader.read(), 5)
            assert rest.count(b"Content-Type: image/png") == 1 and rest.endswith(
                b"--\r\n"
            )
            reader, _ = await get(port, f"/_live/browser/login/{req.id}")
            head = await reader.read()
            assert b"302 Found" in head
            assert f"Location: {r.login_form(req)}".encode() in head
            assert (
                r.login_form(req) == f"https://host.example.ts.net:8454/login/{req.id}/"
            )
            nobody = "lr-" + "0" * 32
            reader, _ = await get(port, f"/_live/browser/login/{nobody}.png")
            head, _, body = (await reader.read()).partition(b"\r\n\r\n")
            assert b"image/png" in head and Image.open(io.BytesIO(body)).format == "PNG"
            reader, _ = await get(port, f"/_live/browser/login/{nobody}")
            head, _, body = (await reader.read()).partition(b"\r\n\r\n")
            assert b"200 OK" in head and b"isn&#x27;t known here" in body
            reader, _ = await get(port, "/_live/browser/login/lr-short.png")
            assert b"404" in await reader.read()
        finally:
            server.close()
            await podman.close()

    asyncio.run(main())


def test_every_state_of_a_login_request_has_a_card():
    from browser.tabs import LoginRequest

    req = LoginRequest("lr-x", "career", "7", "bw-x", ["linkedin.com"], "https://x/", 0)
    for state in live.ASKED:
        image = Image.open(io.BytesIO(live.asked_picture(req, state)))
        assert image.format == "PNG"
        light = Image.open(io.BytesIO(live.asked_picture(req, state, "light")))
        assert light.convert("RGB").getpixel((800, 20)) == THEMES["light"].panel


async def ask(port, path, method="GET", key=None, headers=""):
    """A request with the key a client sends, if any -> (head, JSON body, else the body)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    auth = f"Authorization: Bearer {key}\r\n" if key is not None else ""
    writer.write(f"{method} {path} HTTP/1.1\r\nHost: h\r\n{auth}{headers}\r\n".encode())
    await writer.drain()
    head, _, body = (await reader.read()).partition(b"\r\n\r\n")
    return head, json.loads(body) if b"application/json" in head else body


async def good_key(key):
    """chats.Check for a client whose key is GOOD."""
    return None if key == "GOOD" else (403, "No valid api key found.")


async def lookup(workspace, slug):
    """chats.Lookup for career's chats chat-7 and chat-8, and its main chat."""
    return {
        ("career", "chat-7"): "7",
        ("career", "chat-8"): "8",
        ("career", None): "default",
    }.get((workspace, slug))


def test_a_client_with_a_key_gets_its_chats_cards_and_how_they_stand(tmp_path):
    from browser.chats import Chats

    checked = []

    async def check(key):
        checked.append(key)
        return await good_key(key)

    async def main():
        podman = FakePodman()
        r = Runner(config(tmp_path), podman=podman, now=Clock())
        server = await live.Live(r, Chats(r, check, lookup)).serve(0)
        port = server.sockets[0].getsockname()[1]
        cards = "https://host.example.ts.net:8445/_live/browser"
        try:
            route = "/_live/browser/chat/career/chat-7"
            head, body = await ask(port, route)
            assert b"401" in head and body == {"error": "No valid api key found."}
            assert b"Access-Control-Allow-Origin: *" in head and checked == []
            head, body = await ask(port, route, key="BAD")
            assert b"403" in head and body == {"error": "No valid api key found."}
            head, _ = await ask(port, route, "OPTIONS")  # a web client's preflight
            assert (
                b"204" in head
                and b"Access-Control-Allow-Headers: Authorization" in head
            )
            head, _ = await ask(port, route, "POST", key="GOOD")
            assert b"405" in head
            head, body = await ask(port, "/chat/career/nope", key="GOOD")
            assert b"404" in head and body == {"error": "No such chat."}
            # A chat that hasn't used the browser, and one that has, the prefix stripped.
            head, body = await ask(port, route, key="GOOD")
            assert b"200 OK" in head and b"application/json" in head
            assert body == {"tab": None, "logins": []}
            await r.op_open(scope(), "https://linkedin.com/login")
            tab = r.threads[("career", "7")]
            head, body = await ask(port, "/chat/career/chat-7", key="GOOD")
            assert body == {
                "tab": {
                    "card": f"{cards}/{tab.id}.jpg",
                    "page": f"{cards}/{tab.id}",
                    "frame": f"{cards}/chat/career/chat-7/card.jpg",
                    "state": "idle",  # an op called here, not through reply
                    "title": r.subject(tab),
                    "last": tab.last,
                },
                "logins": [],
            }
            asked = await r.op_ask_login(scope())
            _, body = await ask(port, route, key="GOOD")
            assert body["tab"]["state"] == "waiting"
            assert body["logins"] == [
                {
                    "card": f"{cards}/login/{asked['request']}.png",
                    "page": f"{cards}/login/{asked['request']}",
                    "site": "linkedin.com",
                    "state": "waiting",
                }
            ]
            r.decline(r.asked[asked["request"]])
            _, body = await ask(port, route, key="GOOD")
            assert body["logins"][0]["state"] == "declined"
            _, body = await ask(port, "/chat/career/chat-8", key="GOOD")
            assert body == {"tab": None, "logins": []}  # another chat's are its own
            # The main chat, which has no thread: the workspace's route alone.
            _, body = await ask(port, "/chat/career", key="GOOD")
            assert body == {"tab": None, "logins": []}
            await r.op_open(scope(thread="default"), "https://example.com/")
            main = r.threads[("career", "default")]
            _, body = await ask(port, "/_live/browser/chat/career", key="GOOD")
            assert body["tab"]["page"] == f"{cards}/{main.id}"
            assert body["tab"]["frame"] == f"{cards}/chat/career/card.jpg"
            head, body = await ask(port, "/chat/nowhere", key="GOOD")
            assert b"404" in head and body == {"error": "No such chat."}
            await r.stop("career")
            _, body = await ask(port, route, key="GOOD")
            assert body["tab"]["state"] == "closed"
        finally:
            server.close()
            await podman.close()

    asyncio.run(main())


def test_a_client_with_a_key_gets_its_chats_card_from_any_origin(tmp_path):
    """The chat's card.jpg is the tab's card as it is now, one JPEG any origin may read, for
    a key, with an ETag, and asking for it watches the tab; the card's own address still
    says nothing of CORS."""
    from browser.chats import Chats

    async def main():
        podman = FakePodman()
        clock = Clock()
        r = Runner(config(tmp_path), podman=podman, now=clock)
        server = await live.Live(r, Chats(r, good_key, lookup)).serve(0)
        port = server.sockets[0].getsockname()[1]
        route = "/_live/browser/chat/career/chat-7/card.jpg"
        try:
            head, body = await ask(port, route)  # refused as the JSON is, CORS and all
            assert b"401" in head and body == {"error": "No valid api key found."}
            assert b"Access-Control-Allow-Origin: *" in head
            head, body = await ask(port, route, key="GOOD")  # no tab yet
            assert b"404" in head and body == {"error": "This chat has no browser tab."}
            await r.op_open(scope(), "https://linkedin.com/login")
            tab = r.threads[("career", "7")]
            session = r.sessions["career"]
            assert not r.watched(session)
            head, body = await ask(port, route, key="GOOD")
            assert b"200 OK" in head and b"Content-Type: image/jpeg" in head
            assert b"Access-Control-Allow-Origin: *" in head
            assert b"Cache-Control: private, no-cache" in head
            image = Image.open(io.BytesIO(body))
            assert image.format == "JPEG" and image.width == live.WIDTH
            assert r.watched(session)  # for a while after the client asked
            clock.t += 11
            assert not r.watched(session)
            # Asked again with its ETag, an unchanged card is a 304 without it.
            tag = head.split(b"ETag: ")[1].split(b"\r\n")[0].decode()
            again = f"If-None-Match: {tag}\r\n"
            head, body = await ask(port, route, key="GOOD", headers=again)
            assert b"304 Not Modified" in head and body == b""
            assert b"Content-Length" not in head and b"Access-Control" in head
            tab.moved("Clicked e1")
            head, _ = await ask(port, route, key="GOOD", headers=again)
            assert b"200 OK" in head and tag.encode() not in head
            light = THEMES["light"].panel
            _, body = await ask(port, f"{route}?theme=light", key="GOOD")
            corner = Image.open(io.BytesIO(body)).convert("RGB").getpixel((40, 8))
            assert all(abs(x - y) <= 6 for x, y in zip(corner, light, strict=True))
            # The main chat's, from the workspace's route.
            head, _ = await ask(port, "/chat/career/card.jpg", key="GOOD")
            assert b"404" in head
            await r.op_open(scope(thread="default"), "https://example.com/")
            head, _ = await ask(port, "/chat/career/card.jpg", key="GOOD")
            assert b"200 OK" in head and b"image/jpeg" in head
            # The card's own address, which needs no key, gives no other origin a read.
            reader, writer = await get(port, f"/_live/browser/{tab.id}.jpg")
            head, part, _ = await first_frame(reader)
            writer.close()
            assert b"image/jpeg" in part and b"Access-Control-Allow-Origin" not in head
        finally:
            server.close()
            await podman.close()

    asyncio.run(main())


def test_a_key_is_checked_with_anythingllm_and_a_good_one_remembered(monkeypatch):
    from browser import chats

    asked, answers, now = [], {"GOOD": 200, "BAD": 403, "ODD": 500}, [0.0]

    def get_json(url, headers):
        asked.append((url, headers["Authorization"]))
        return answers[headers["Authorization"].removeprefix("Bearer ")], {}

    monkeypatch.setattr(chats, "get_json", get_json)
    check = chats.KeyCheck("http://all.example", now=lambda: now[0])

    async def main():
        assert await check("GOOD") is None
        assert await check("GOOD") is None
        assert asked == [("http://all.example/api/v1/auth", "Bearer GOOD")]
        assert b"GOOD" not in b"".join(check.good)  # kept by its hash
        assert await check("BAD") == (403, "No valid api key found.")
        assert (await check("ODD"))[0] == 502
        now[0] = 61
        assert await check("GOOD") is None and len(asked) == 4  # asked again

    asyncio.run(main())


def test_a_threads_id_comes_from_its_workspaces_list(monkeypatch, tmp_path):
    from browser import chats

    lists, now = [], [0.0]
    threads = [{"id": 7, "slug": "chat-7"}]

    def get_json(url, headers):
        lists.append(url)
        if "nowhere" in url:
            return 404, None  # AnythingLLM's answer for no such workspace
        if "broken" in url:
            return 500, None
        return 200, {"threads": list(threads), "defaultThreadChatCount": 0}

    monkeypatch.setattr(chats, "get_json", get_json)
    monkeypatch.setattr(chats.hostenv, "anythingllm_headers", lambda *a, **k: {})
    ids = chats.ThreadIds("http://all.example", tmp_path / ".env", now=lambda: now[0])

    async def main():
        assert await ids("career", "chat-7") == "7"
        assert await ids("career", "chat-7") == "7"
        assert lists == ["http://all.example/api/workspace/career/threads"]
        threads.append({"id": 8, "slug": "chat-8"})
        assert await ids("career", "chat-8") == "8"  # a chat made since: asked again
        assert await ids("career", "gone") is None
        assert await ids("nowhere", "chat-7") is None
        assert len(lists) == 4
        assert (
            await ids("career", None) == "default"
        )  # the main chat, from the list kept
        assert await ids("nowhere", None) is None  # no workspace, no main chat
        assert len(lists) == 5
        with pytest.raises(chats.Unavailable):  # AnythingLLM's trouble isn't "no chat"
            await ids("broken", "chat-7")

    asyncio.run(main())
