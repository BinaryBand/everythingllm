"""AnythingLLM's developer API through the relay (relay.proxy), against an AnythingLLM
stood in for by an httpx MockTransport."""

import asyncio
import contextlib
import logging

import httpx
from relay.app import Config, create_app
from relay.proxy import Proxy
from relay.upstream import UNREACHABLE

KEY = "allm-key-0123456789"
TOKEN = "relay-token-abcdef"


class Body(httpx.AsyncByteStream):
    """A body as a real transport gives one: streamed, not read yet (a Response made with
    `content=` comes already read, and so can't be streamed again)."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self):
        yield self.data


def go(coro):
    return asyncio.run(asyncio.wait_for(coro, 10))


@contextlib.asynccontextmanager
async def running(tmp_path, handler):
    """The app started as uvicorn would, with `handler` as AnythingLLM, and a client that
    sends the relay's token."""
    app = create_app(
        Config(api_key=KEY, token=TOKEN, database=tmp_path / "relay.db"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://relay",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client,
    ):
        yield client


def test_a_call_goes_on_with_the_key_and_comes_back_as_it_was(tmp_path):
    seen = []

    def allm(request):
        seen.append(request)
        return httpx.Response(
            207,
            headers=[
                ("Set-Cookie", "a=1"),
                ("Set-Cookie", "b=2"),
                ("X-Thing", "kept"),
                ("Connection", "close"),
            ],
            stream=Body(b'{"ok": true}'),
        )

    async def main():
        async with running(tmp_path, allm) as client:
            return await client.post(
                "/api/v1/workspace/my%20space/update?x=1&y=%2F",
                content=b'{"name": "n"}',
                headers={
                    "Content-Type": "application/json",
                    "Accept-Encoding": "br",
                    "User-Agent": "nilson/1",
                },
            )

    r = go(main())
    [request] = seen
    assert request.method == "POST"
    assert (
        str(request.url)
        == "http://127.0.0.1:3001/api/v1/workspace/my%20space/update?x=1&y=%2F"
    )
    assert request.content == b'{"name": "n"}'
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers.get_list("authorization") == [f"Bearer {KEY}"]
    assert request.headers["user-agent"] == "nilson/1"  # not the proxy's httpx
    assert request.headers["content-type"] == "application/json"
    assert request.headers["host"] == "127.0.0.1:3001"
    assert request.headers.get_list("accept-encoding") == ["br"]
    assert r.status_code == 207
    assert r.content == b'{"ok": true}'
    assert r.headers.get_list("set-cookie") == ["a=1", "b=2"]
    assert r.headers["x-thing"] == "kept"
    assert "connection" not in r.headers


def test_a_get_sends_no_body_and_an_encoded_answer_stays_encoded(tmp_path):
    import gzip

    seen = []
    packed = gzip.compress(b'{"workspaces": []}')

    def allm(request):
        seen.append(request)
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
            stream=Body(packed),
        )

    async def main():
        async with running(tmp_path, allm) as client:
            return await client.get(
                "/api/v1/workspaces", headers={"Accept-Encoding": "gzip"}
            )

    r = go(main())
    [request] = seen
    assert request.content == b""
    assert "content-length" not in request.headers
    assert "transfer-encoding" not in request.headers
    assert request.headers["accept-encoding"] == "gzip"
    assert r.json() == {"workspaces": []}  # httpx decoded the gzip it was sent


def test_server_sent_events_pass_through(tmp_path):
    events = b'data: {"textResponse": "Hi"}\n\ndata: {"close": true}\n\n'

    def allm(request):
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=Body(events)
        )

    async def main():
        async with running(tmp_path, allm) as client:
            return await client.post(
                "/api/v1/workspace/w/stream-chat", json={"message": "Hi?"}
            )

    r = go(main())
    assert r.headers["content-type"] == "text/event-stream"
    assert r.content == events


def test_the_proxy_needs_the_token_and_never_shows_the_key(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    calls = []

    def allm(request):
        calls.append(request)
        return httpx.Response(200, stream=Body(b'{"authenticated": true}'))

    async def main():
        async with running(tmp_path, allm) as client:
            refused = [
                await client.get("/api/v1/auth", headers=headers)
                for headers in (
                    {"Authorization": ""},
                    {"Authorization": "Bearer nope"},
                    {"Authorization": f"Bearer {KEY}"},
                )
            ]
            return refused, await client.get("/api/v1/auth")

    refused, ok = go(main())
    assert [r.status_code for r in refused] == [401, 401, 401]
    assert len(calls) == 1
    assert ok.json() == {"authenticated": True}
    for text in [*(r.text for r in refused), ok.text, caplog.text]:
        assert KEY not in text and TOKEN not in text


def test_only_the_developer_api_is_proxied(tmp_path):
    calls = []

    def allm(request):
        calls.append(request)
        return httpx.Response(200)

    async def main():
        async with running(tmp_path, allm) as client:
            return [
                (await client.get(path)).status_code
                for path in ("/api/system", "/api/v1", "/api/v2/x", "/workspaces")
            ]

    # /api/v1 is redirected to /api/v1/, which isn't a route either.
    assert go(main()) == [404, 307, 404, 404]
    assert calls == []


def test_an_unreachable_anythingllm_is_a_502(tmp_path, caplog):
    def allm(request):
        raise httpx.ConnectError(f"can't reach {request.url}")

    async def main():
        async with running(tmp_path, allm) as client:
            return await client.get("/api/v1/workspaces")

    r = go(main())
    assert r.status_code == 502
    assert r.json() == {"error": UNREACHABLE}
    assert "ConnectError" in caplog.text


def test_a_client_that_leaves_closes_the_call_to_anythingllm():
    """Straight against the ASGI app, since httpx's ASGITransport reads a whole response
    before it returns and so can't leave halfway."""
    closed = asyncio.Event()
    first = asyncio.Event()

    class Endless(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                yield b"data: {}\n\n"
                await asyncio.Event().wait()
            finally:
                closed.set()

    def allm(request):
        return httpx.Response(200, stream=Endless())

    async def main():
        client = httpx.AsyncClient(transport=httpx.MockTransport(allm))
        proxy = Proxy(client, "http://allm", KEY)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/x",
            "raw_path": b"/api/v1/x",
            "query_string": b"",
            "headers": [],
        }
        sent = []

        async def receive():
            await first.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if message.get("body"):
                first.set()

        await proxy(scope, receive, send)
        await asyncio.wait_for(closed.wait(), 1)
        await client.aclose()
        return sent

    sent = go(main())
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[1]["more_body"] is True  # never finished, as it shouldn't be


def test_an_answer_broken_off_is_left_unfinished():
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"part"
            raise httpx.ReadError("gone")

    def allm(request):
        return httpx.Response(200, stream=Broken())

    async def main():
        client = httpx.AsyncClient(transport=httpx.MockTransport(allm))
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/x",
            "query_string": b"",
            "headers": [],
        }
        sent = []

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            sent.append(message)

        await Proxy(client, "http://allm", KEY)(scope, receive, send)
        await client.aclose()
        return sent

    sent = go(main())
    assert [m.get("more_body") for m in sent] == [None, True]
