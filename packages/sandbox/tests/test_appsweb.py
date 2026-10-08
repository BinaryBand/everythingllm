"""The apps server (sandbox.appsweb): an app's live card and the way to its page."""

import asyncio
import json

import pytest
from sandbox import appsweb
from test_runner import A, cfg, make, project  # noqa: F401 - cfg is a fixture


@pytest.fixture
def r(cfg, tmp_path):  # noqa: F811
    cfg.app_state = tmp_path / "data" / "apps"
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
