"""Live chat images: an HTTP response that keeps replacing an image for as long as its
connection stays open, so a Markdown image in the chat shows a job's progress with no
script.

The response is `multipart/x-mixed-replace` (server push): every part is a whole PNG, and
the browser shows the newest part in the `<img>`. Browsers still support it for images,
and `tailscale serve` passes each part on as it comes. When the response ends, the image
stays on its last frame; reloading the chat asks again.

These are helpers for a service's own small asyncio server (research.live is one): it
reads the request line, then either pushes frames or sends one plain response. There's
no framework: GET only, no keep-alive, every response closes its connection.
"""

import asyncio
from collections.abc import AsyncIterator

BOUNDARY = b"frame"
MAX_HEAD = 16 * 1024  # a request's line and headers; tailscale serve adds a few


class BadRequest(Exception):
    pass


async def read_request(reader: asyncio.StreamReader) -> tuple[str, str]:
    """The request's method and path (with its query cut off), once its headers are in.
    Raises BadRequest for anything that isn't an HTTP/1 request line, or is too big."""
    read = 0
    try:
        line = await reader.readuntil(b"\n")
        read += len(line)
        while True:
            header = await reader.readuntil(b"\n")
            read += len(header)
            if read > MAX_HEAD:
                raise BadRequest("request too big")
            if header in (b"\r\n", b"\n"):
                break
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as e:
        raise BadRequest("incomplete request") from e
    parts = line.decode("latin-1").split()
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise BadRequest("not an HTTP/1 request")
    method, target, _ = parts
    return method, target.split("?", 1)[0]


def head(status: str, headers: dict[str, str]) -> bytes:
    lines = [f"HTTP/1.1 {status}"]
    lines += [f"{k}: {v}" for k, v in {**headers, "Connection": "close"}.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


async def send(
    writer: asyncio.StreamWriter,
    status: str,
    body: bytes = b"",
    content_type: str = "text/plain; charset=utf-8",
    headers: dict[str, str] | None = None,
) -> None:
    """One plain response, then close."""
    try:
        writer.write(
            head(
                status,
                {
                    "Content-Type": content_type,
                    "Content-Length": str(len(body)),
                    "Cache-Control": "no-store",
                    **(headers or {}),
                },
            )
            + body
        )
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        await close(writer)


async def push(
    writer: asyncio.StreamWriter,
    frames: AsyncIterator[bytes],
    content_type: str = "image/png",
) -> bool:
    """Push each image from `frames` (PNGs, or whatever `content_type` says) as the image's
    newest frame, until they run out or the client goes; then close. True when every frame
    went out, False when the client left first (the frames are closed either way)."""
    try:
        writer.write(
            head(
                "200 OK",
                {
                    "Content-Type": f"multipart/x-mixed-replace; boundary={BOUNDARY.decode()}",
                    "Cache-Control": "no-store",
                    "X-Accel-Buffering": "no",
                },
            )
        )
        async for image in frames:
            writer.write(
                b"--%s\r\nContent-Type: %s\r\nContent-Length: %d\r\n\r\n%s\r\n"
                % (BOUNDARY, content_type.encode(), len(image), image)
            )
            await writer.drain()
        writer.write(b"--%s--\r\n" % BOUNDARY)
        await writer.drain()
        return True
    except (ConnectionError, OSError):
        return False
    finally:
        aclose = getattr(frames, "aclose", None)
        if aclose:
            await aclose()
        await close(writer)


async def close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, OSError):
        pass
