"""The apps server (sandbox.appsweb): an app's live card and the way to its page."""

import asyncio
import json
from unittest.mock import ANY

import pytest
from sandbox import appsweb
from sandbox.errors import Busy
from test_runner import A, cfg, make, project  # noqa: F401 - cfg is a fixture


@pytest.fixture
def r(cfg):  # noqa: F811
    return make(cfg)


def ADD(text):
    return {
        "op": "add",
        "path": "/data/items/-",
        "value": {"text": text, "done": False},
    }


def TICK(i):
    return {"op": "replace", "path": f"/data/items/{i}/done", "value": True}


async def started(r):
    server = await appsweb.AppsWeb(r).serve(0)
    return server, server.sockets[0].getsockname()[1]


async def ask(port, line: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(line + b"\r\nHost: x\r\n\r\n")
    await writer.drain()
    data = await reader.read()
    writer.close()
    return data


def test_the_card_address_without_png_leads_to_the_page(r):
    async def main():
        server, port = await started(r)
        try:
            for path in (b"/_live/apps/career/groceries", b"/career/groceries"):
                head = await ask(port, b"GET " + path + b" HTTP/1.1")
                assert head.startswith(b"HTTP/1.1 302 Found")
                assert (
                    b"Location: https://ws.example/career/apps/groceries/\r\n" in head
                )
            assert (
                b"404" in (await ask(port, b"GET /_live/apps/Career/x HTTP/1.1"))[:20]
            )
            assert (
                b"405"
                in (await ask(port, b"DELETE /_live/apps/career/x HTTP/1.1"))[:20]
            )
        finally:
            server.close()

    asyncio.run(main())


def test_the_card_is_live_and_moves_on_with_the_app(r, cfg, monkeypatch):  # noqa: F811
    monkeypatch.setattr(appsweb, "POLL", 0.05)

    async def frames(reader, n):
        """The first n frames' bodies of a pushed response."""
        out = []
        await reader.readuntil(b"\r\n\r\n")  # the response's head
        while len(out) < n:
            await reader.readuntil(b"--frame\r\n")
            head = await reader.readuntil(b"\r\n\r\n")
            length = int(head.split(b"Content-Length: ")[1].split(b"\r\n")[0])
            body = await reader.readexactly(length)
            if (
                not out or body != out[-1]
            ):  # a frame sent again to be shown is one frame
                out.append(body)
        return out

    async def main():
        await r.op_app(A, "create", "groceries", starter="list")
        await r.op_app(A, "do", "groceries", patch=[ADD("Oat milk")])
        server, port = await started(r)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET /_live/apps/career/groceries.png?theme=light HTTP/1.1\r\n\r\n"
        )
        await writer.drain()
        try:
            first = asyncio.ensure_future(frames(reader, 3))
            await asyncio.sleep(0.3)
            await r.op_app(
                A,
                "do",
                "groceries",
                patch=[
                    {"op": "test", "path": "/items/0/text", "value": "Oat milk"},
                    TICK(0),
                ],
            )
            await asyncio.sleep(0.3)
            # A run's own edit to the data, which no op announces.
            data_file = project(cfg, A) / "apps" / "groceries" / "data.json"
            data = json.loads(data_file.read_text())
            data["title"] = "Weekend groceries"
            data_file.write_text(json.dumps(data))
            shots = await asyncio.wait_for(first, 5)
            assert all(s[:8] == b"\x89PNG\r\n\x1a\n" for s in shots)
            assert len(set(shots)) == 3
        finally:
            writer.close()
            server.close()

    asyncio.run(main())


def test_an_app_that_is_gone_shows_so(r):
    async def main():
        await r.op_app(A, "create", "todo", starter="list")
        await r.op_app(A, "delete", "todo")
        server, port = await started(r)
        try:
            for path in (
                b"/_live/apps/career/todo.png",
                b"/_live/apps/nowhere/todo.png",
            ):
                answer = await ask(port, b"GET " + path + b" HTTP/1.1")
                assert answer.startswith(b"HTTP/1.1 200 OK")
                assert b"\x89PNG" in answer
        finally:
            server.close()

    asyncio.run(main())


def test_only_loopback_is_answered(r, monkeypatch):
    monkeypatch.setattr(appsweb.hostrpc, "local_peer", lambda peer, own: False)

    async def main():
        server, port = await started(r)
        try:
            answer = await ask(port, b"GET /_live/apps/career/x.png HTTP/1.1")
            assert answer.startswith(b"HTTP/1.1 403")
        finally:
            server.close()

    asyncio.run(main())


async def post(
    port, path: str, body: bytes, length: bool = True
) -> tuple[int, dict, bytes]:
    head = f"POST {path} HTTP/1.1\r\nHost: x\r\nOrigin: null\r\nContent-Type: text/plain\r\n"
    if length:
        head += f"Content-Length: {len(body)}\r\n"
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head.encode() + b"\r\n" + body)
    await writer.drain()
    answer = await reader.read()
    writer.close()
    top, _, payload = answer.partition(b"\r\n\r\n")
    assert b"\r\nAccess-Control-Allow-Origin: null\r\n" in top  # the page can read it
    return int(top.split()[1]), json.loads(payload or b"{}"), top


def write(token, *ops, version=None) -> bytes:
    body = {"token": token, "patch": list(ops)}
    if version is not None:
        body["version"] = version
    return json.dumps(body).encode()


async def made(r, items=("Oat milk", "Eggs")):
    await r.op_app(A, "create", "groceries", starter="list")
    await r.op_app(A, "do", "groceries", patch=[ADD(i) for i in items])
    return r.app_tokens.held("career", "groceries")[0]


def test_the_page_writes_back_with_its_token_and_version(r, cfg):  # noqa: F811
    async def main():
        token = await made(r)
        server, port = await started(r)
        try:
            path = "/_apps/career/groceries/ops"
            status, body, _ = await post(port, path, write(token, TICK(0), version=2))
            assert (status, body) == (200, {"version": 3})
            status, body, _ = await post(
                port, "/career/groceries/ops", write(token, ADD("Rye"), version=3)
            )
            assert (status, body) == (200, {"version": 4})
            # Written against a version that's gone: the app as it is now, to rebase on.
            status, body, _ = await post(port, path, write(token, TICK(1), version=3))
            assert status == 409 and body["conflict"] and body["version"] == 4
            assert [i["text"] for i in body["data"]["items"]] == [
                "Oat milk",
                "Eggs",
                "Rye",
            ]
            # No version: applied to whatever it is now, a change by place only with a
            # test of its item.
            status, body, _ = await post(port, path, write(token, TICK(1)))
            assert (
                status == 409 and body["conflict"] and "by its place" in body["error"]
            )
            guard = {"op": "test", "path": "/items/1/text", "value": "Eggs"}
            assert (await post(port, path, write(token, guard, TICK(1))))[0] == 200
            assert (await post(port, path, write("forged", TICK(1))))[0] == 403
            # A tab with the page before page.html changed: reload.
            (project(cfg, A) / "apps" / "groceries" / "page.html").write_text(
                "<script>App.start(/*APP*/null)</script>"
            )
            await r.op_app(A, "show", "groceries")
            status, body, _ = await post(port, path, write(token, TICK(2)))
            assert (status, body["reload"]) == (409, True)
            page = await ask(port, b"OPTIONS " + path.encode() + b" HTTP/1.1")
            assert (
                page.startswith(b"HTTP/1.1 204")
                and b"Access-Control-Allow-Origin: null" in page
            )
        finally:
            server.close()

    asyncio.run(main())


def test_what_the_write_back_refuses(r, monkeypatch):
    async def main():
        token = await made(r)
        server, port = await started(r)
        path = "/_apps/career/groceries/ops"
        try:
            with monkeypatch.context() as m:
                m.setattr(appsweb, "MAX_BODY", 1000)
                assert (await post(port, path, b"x" * 1001))[0] == 413
            assert (await post(port, path, write(token, TICK(0)), length=False))[
                0
            ] == 411
            assert (await post(port, path, b"not json"))[0] == 400
            assert (await post(port, path, json.dumps({"patch": []}).encode()))[
                0
            ] == 400
            status, body, _ = await post(
                port,
                path,
                json.dumps({"token": token, "patch": {"op": "add"}}).encode(),
            )
            assert (status, body["error"]) == (400, "not a patch")
            status, body, _ = await post(port, path, write(token, {"op": "shuffle"}))
            assert status == 400 and "op must be one of" in body["error"]
            status, body, _ = await post(
                port, path, write(token, {"op": "add", "path": "", "value": 1})
            )
            assert status == 400 and "JSON pointer" in body["error"]
            assert (await post(port, "/_apps/nowhere/groceries/ops", write(token)))[
                0
            ] == 404
            # The token is checked first: an app that isn't there has none to match.
            assert (await post(port, "/_apps/career/gone/ops", write(token, TICK(0))))[
                0
            ] == 403

            def busy(workspace):
                raise Busy("code is still running in this workspace")

            with monkeypatch.context() as m:
                m.setattr(r, "idle", busy)
                assert (await post(port, path, write(token, TICK(0))))[0] == 409
            monkeypatch.setattr(appsweb, "RATE", (2, 10.0))
            fresh = appsweb.AppsWeb(r)
            assert fresh.allowed(("career", "x")) and fresh.allowed(("career", "x"))
            assert not fresh.allowed(("career", "x"))
            assert fresh.allowed(("career", "other"))
        finally:
            server.close()

    asyncio.run(main())


def test_sync_answers_when_the_app_changes_or_after_its_wait(r, monkeypatch):
    monkeypatch.setattr(appsweb, "SYNC_WAIT", 0.3)
    monkeypatch.setattr(appsweb, "POLL", 0.05)

    async def main():
        token = await made(r)
        server, port = await started(r)
        path = "/_apps/career/groceries/sync"
        try:
            # Behind already: at once, the app as it is.
            status, body, _ = await post(
                port, path, json.dumps({"token": token, "since": 1}).encode()
            )
            assert (
                status == 200
                and body["version"] == 2
                and len(body["data"]["items"]) == 2
            )
            assert set(body) == {"title", "version", "card", "data"}
            # Up to date and nothing happens: the version, after the wait.
            status, body, _ = await post(
                port, path, json.dumps({"token": token, "since": 2}).encode()
            )
            assert (status, body) == (200, {"version": 2})
            # Up to date, then the agent changes it: the change, before the wait is out.
            waiting = asyncio.ensure_future(
                post(port, path, json.dumps({"token": token, "since": 2}).encode())
            )
            await asyncio.sleep(0.05)
            await r.op_app(A, "do", "groceries", patch=[ADD("Rye")])
            status, body, _ = await asyncio.wait_for(waiting, 2)
            assert (
                status == 200
                and body["version"] == 3
                and len(body["data"]["items"]) == 3
            )
            status, _, _ = await post(
                port, path, json.dumps({"token": "forged", "since": 3}).encode()
            )
            assert status == 403
        finally:
            server.close()

    asyncio.run(main())


def test_sync_holds_only_so_many_pages_of_an_app(r, monkeypatch):
    monkeypatch.setattr(appsweb, "SYNC_WAIT", 0.5)
    monkeypatch.setattr(appsweb, "SYNC_WAITERS", 2)

    async def main():
        token = await made(r)
        server, port = await started(r)
        path = "/_apps/career/groceries/sync"
        body = json.dumps({"token": token, "since": 2}).encode()
        try:
            held = [asyncio.ensure_future(post(port, path, body)) for _ in range(2)]
            await asyncio.sleep(0.1)
            assert (await post(port, path, body))[0] == 429
            assert [s for s, _, _ in await asyncio.gather(*held)] == [200, 200]
            assert (await post(port, path, body))[0] == 200  # their places freed
        finally:
            server.close()

    asyncio.run(main())


def test_sync_answers_an_unchanged_app_from_a_stat(r, monkeypatch):
    monkeypatch.setattr(appsweb, "SYNC_WAIT", 0.1)

    async def main():
        token = await made(r)
        reads = []
        read_app = r.read_app
        monkeypatch.setattr(
            r, "read_app", lambda s, n: reads.append(n) or read_app(s, n)
        )
        server, port = await started(r)
        path = "/_apps/career/groceries/sync"
        body = json.dumps({"token": token, "since": 2}).encode()
        try:
            for _ in range(3):
                assert await post(port, path, body) == (200, {"version": 2}, ANY)
            assert reads == ["groceries"]  # the first, to learn its version
        finally:
            server.close()

    asyncio.run(main())


def test_a_card_says_when_its_data_is_broken_and_comes_back_when_fixed(
    r,
    cfg,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(appsweb, "POLL", 0.05)

    async def main():
        await made(r)
        web = appsweb.AppsWeb(r)
        stream = web.frames(r.app_scope("career"), "groceries", "light")
        first = await anext(stream)
        data_file = project(cfg, A) / "apps" / "groceries" / "data.json"
        good = data_file.read_text()
        data_file.write_text('{"title": "no data"}')
        broken = await asyncio.wait_for(anext(stream), 5)
        assert broken == appsweb.gone_card("light", appsweb.BROKEN)
        assert broken != appsweb.gone_card("light")  # not "deleted"
        data_file.write_text(good)
        assert await asyncio.wait_for(anext(stream), 5) == first
        await stream.aclose()

    asyncio.run(main())
