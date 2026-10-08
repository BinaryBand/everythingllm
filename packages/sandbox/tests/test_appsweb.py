"""The apps server (sandbox.appsweb): an app's live card and the way to its page."""

import asyncio
import json

import pytest
from sandbox import appsweb
from sandbox.errors import Busy
from test_runner import A, cfg, make, project  # noqa: F401 - cfg is a fixture


@pytest.fixture
def r(cfg):  # noqa: F811
    return make(cfg)


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
        await r.op_app(A, "create", "groceries", args={"item": "Oat milk"})
        server, port = await started(r)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET /_live/apps/career/groceries.png?theme=light HTTP/1.1\r\n\r\n"
        )
        await writer.drain()
        try:
            first = asyncio.ensure_future(frames(reader, 3))
            await asyncio.sleep(0.3)
            await r.op_app(A, "do", "groceries", op="check", args={"item": "oat milk"})
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
        await r.op_app(A, "create", "todo")
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


def op(token, name="check", **args) -> bytes:
    return json.dumps({"token": token, "op": name, "args": args}).encode()


def test_the_page_writes_back_with_its_current_token(r):
    async def main():
        await r.op_app(A, "create", "groceries", args={"items": ["Oat milk", "Eggs"]})
        first, _ = r.app_tokens.held("career", "groceries")
        server, port = await started(r)
        try:
            path = "/_apps/career/groceries/ops"
            status, body, _ = await post(port, path, op(first, item=1))
            assert status == 200 and body["data"]["version"] == 2
            assert body["data"]["items"][0]["done"] and body["token"] != first
            # The same page's next op, with the token it was given.
            status, body, _ = await post(
                port, "/career/groceries/ops", op(body["token"], "add", item="Rye")
            )
            assert status == 200 and len(body["data"]["items"]) == 3
            # A tab that wasn't told: reload. Anyone else: no.
            status, body, _ = await post(port, path, op(first, item=2))
            assert (status, body["reload"]) == (409, True)
            assert (await post(port, path, op("forged", item=2)))[0] == 403
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
        await r.op_app(A, "create", "todo", args={"item": "a"})
        token, _ = r.app_tokens.held("career", "todo")
        server, port = await started(r)
        path = "/_apps/career/todo/ops"
        try:
            assert (await post(port, path, b"x" * 5000))[0] == 413
            assert (await post(port, path, op(token), length=False))[0] == 411
            assert (await post(port, path, b"not json"))[0] == 400
            assert (await post(port, path, json.dumps({"op": "check"}).encode()))[
                0
            ] == 400
            status, body, _ = await post(port, path, op(token, "shuffle"))
            assert status == 400 and "op must be one of" in body["error"]
            assert (await post(port, "/_apps/nowhere/todo/ops", op(token)))[0] == 404
            assert (await post(port, "/_apps/career/gone/ops", op(token)))[0] == 404

            def busy(workspace):
                raise Busy("code is still running in this workspace")

            with monkeypatch.context() as m:
                m.setattr(r, "idle", busy)
                assert (await post(port, path, op(token, item="a")))[0] == 409
            monkeypatch.setattr(appsweb, "RATE", (2, 10.0))
            fresh = appsweb.AppsWeb(r)
            assert fresh.allowed(("career", "todo")) and fresh.allowed(
                ("career", "todo")
            )
            assert not fresh.allowed(("career", "todo"))
            assert fresh.allowed(("career", "other"))
        finally:
            server.close()

    asyncio.run(main())
