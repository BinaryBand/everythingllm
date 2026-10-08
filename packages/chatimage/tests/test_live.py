import asyncio

from chatimage import live


async def serve(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def get(port, request=b"GET /x.png?v=1 HTTP/1.1\r\nHost: h\r\n\r\n"):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    data = await reader.read()
    writer.close()
    return data


def parts(body: bytes) -> list[bytes]:
    """The PNGs in a multipart/x-mixed-replace body, checking its framing."""
    assert body.endswith(b"--frame--\r\n")
    found = []
    for chunk in body.split(b"--frame\r\n")[1:]:
        head, _, rest = chunk.partition(b"\r\n\r\n")
        length = int(
            dict(line.split(b": ") for line in head.split(b"\r\n"))[b"Content-Length"]
        )
        found.append(rest[:length])
        assert rest[length : length + 2] == b"\r\n"
    return found


def test_frames_are_pushed_in_order_and_the_stream_ends():
    seen = []

    async def frames():
        for n in range(3):
            yield b"png%d" % n

    async def handler(reader, writer):
        seen.append((await live.read_head(reader))[:3])
        seen.append(await live.push(writer, frames()))

    async def go():
        server, port = await serve(handler)
        async with server:
            return await get(port)

    data = asyncio.run(go())
    head, _, body = data.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200 OK")
    assert b"Content-Type: multipart/x-mixed-replace; boundary=frame" in head
    assert b"Cache-Control: no-store" in head
    assert b"Access-Control-Allow-Origin: *" in head  # a web client may draw it
    assert parts(body) == [b"png0", b"png1", b"png2"]
    assert seen == [("GET", "/x.png", "v=1"), True]


def test_a_push_without_cors_is_only_its_own_origins_to_read():
    async def frames():
        yield b"png0"

    async def handler(reader, writer):
        await live.read_head(reader)
        await live.push(writer, frames(), cors=False)

    async def go():
        server, port = await serve(handler)
        async with server:
            return await get(port)

    head, _, body = asyncio.run(go()).partition(b"\r\n\r\n")
    assert b"Access-Control" not in head
    assert parts(body) == [b"png0"]


def test_a_still_streams_frame_is_followed_by_a_part_so_a_browser_shows_it():
    """Chrome shows a part once the next part's headers are in, so a frame no newer one
    follows is sent again, whole, for a reader to take without waiting for a second one."""
    still = asyncio.Event()

    async def frames():
        yield b"png0"
        await still.wait()  # a page that doesn't move
        yield b"png1"

    async def handler(reader, writer):
        await live.read_head(reader)
        await live.push(writer, frames())

    async def go():
        server, port = await serve(handler)
        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /x.png HTTP/1.1\r\n\r\n")
            await reader.readuntil(b"\r\n\r\n")
            shown = b""
            while shown.count(b"--frame\r\n") < 2 or not shown.endswith(b"\r\n"):
                shown += await asyncio.wait_for(reader.read(1024), live.SETTLE + 2)
            still.set()
            rest = await reader.read()
            writer.close()
            return shown, rest

    shown, rest = asyncio.run(go())
    assert parts(shown + rest) == [b"png0", b"png0", b"png1"]
    assert parts(shown + b"--frame--\r\n") == [b"png0", b"png0"]


def test_frames_that_come_quickly_are_sent_once():
    async def frames():
        for n in range(3):
            await asyncio.sleep(live.SETTLE / 10)
            yield b"png%d" % n

    async def handler(reader, writer):
        await live.read_head(reader)
        await live.push(writer, frames())

    async def go():
        server, port = await serve(handler)
        async with server:
            return await get(port)

    assert parts(asyncio.run(go()).partition(b"\r\n\r\n")[2]) == [
        b"png0",
        b"png1",
        b"png2",
    ]


def test_a_client_that_leaves_closes_the_frames():
    closed = asyncio.Event()
    result = []

    async def frames():
        try:
            while True:
                yield b"x" * 100_000
                await asyncio.sleep(0.01)
        finally:
            closed.set()

    async def handler(reader, writer):
        await live.read_head(reader)
        result.append(await live.push(writer, frames()))

    async def go():
        server, port = await serve(handler)
        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /x.png HTTP/1.1\r\n\r\n")
            await reader.readexactly(1000)
            writer.transport.abort()
            await asyncio.wait_for(closed.wait(), 5)
            for _ in range(100):
                if result:
                    break
                await asyncio.sleep(0.01)

    asyncio.run(go())
    assert result == [False]


def test_a_plain_response_and_a_bad_request():
    async def handler(reader, writer):
        try:
            await live.read_head(reader)
        except live.BadRequest as e:
            return await live.send(writer, "400 Bad Request", str(e).encode())
        await live.send(writer, "302 Found", headers={"Location": "https://h/r/"})

    async def go():
        server, port = await serve(handler)
        async with server:
            return await get(port), await get(port, b"nonsense\r\n\r\n")

    ok, bad = asyncio.run(go())
    assert (
        ok.startswith(b"HTTP/1.1 302 Found\r\n") and b"Location: https://h/r/\r\n" in ok
    )
    assert b"Content-Length: 0\r\n" in ok and b"Connection: close\r\n" in ok
    assert bad.startswith(b"HTTP/1.1 400 Bad Request")
    assert b"Access-Control" not in ok + bad  # only images are anyone's to read


def test_a_single_frame_may_be_read_by_any_origin():
    async def handler(reader, writer):
        await live.read_head(reader)
        await live.send(writer, "200 OK", b"png", "image/png")

    async def go():
        server, port = await serve(handler)
        async with server:
            return await get(port)

    assert b"Access-Control-Allow-Origin: *\r\n" in asyncio.run(go())


def test_a_query_asks_for_a_theme():
    assert live.theme("") == "dark"
    assert live.theme("theme=light") == "light"
    assert live.theme("v=abc&theme=light") == "light"
    assert live.theme("theme=sepia") == "dark"
    assert live.theme("theme=light&theme=dark") == "dark"


class Elsewhere:
    """A connection's writer, as though it came from another container on egress-net."""

    def __init__(self, writer):
        self.writer = writer

    def get_extra_info(self, name, default=None):
        return {"peername": ("10.89.79.50", 40000), "sockname": ("10.89.79.40", 8000)}.get(name, default)

    def __getattr__(self, name):
        return getattr(self.writer, name)


def test_accept_answers_only_a_request_from_here():
    seen = []

    def handler(wrap):
        async def handle(reader, writer):
            head = await live.accept(reader, wrap(writer))
            seen.append(head)
            if head:
                await live.send(writer, "200 OK", b"ok\n")

        return handle

    async def go(wrap, request):
        server, port = await serve(handler(wrap))
        async with server:
            return await get(port, request)

    here = asyncio.run(go(lambda w: w, b"GET /a?b=1 HTTP/1.1\r\nHost: h\r\n\r\n"))
    assert here.startswith(b"HTTP/1.1 200 OK")
    assert seen.pop() == ("GET", "/a", "b=1", {"host": "h"})
    bad = asyncio.run(go(lambda w: w, b"NOPE\r\n\r\n"))
    assert bad.startswith(b"HTTP/1.1 400 Bad Request") and seen.pop() is None
    other = asyncio.run(go(Elsewhere, b"GET /a HTTP/1.1\r\n\r\n"))
    assert other.startswith(b"HTTP/1.1 403 Forbidden") and seen.pop() is None
