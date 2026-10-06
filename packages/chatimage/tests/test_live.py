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
        seen.append(await live.read_request(reader))
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
    assert parts(body) == [b"png0", b"png1", b"png2"]
    assert seen == [("GET", "/x.png"), True]


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
        await live.read_request(reader)
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
            await live.read_request(reader)
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
