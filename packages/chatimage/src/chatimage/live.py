"""Live chat images: an HTTP response that keeps replacing an image for as long as its
connection stays open, so a Markdown image in the chat shows a job's progress with no
script.

The response is `multipart/x-mixed-replace` (server push): every part is a whole PNG, and
the browser shows the newest part in the `<img>`. Browsers still support it for images,
and the machine's route must pass each part on as it comes (no buffering). When the response ends, the image
stays on its last frame; reloading the chat asks again.

Chrome shows a part only once the next part's headers are in, not at its end or at the
delimiter after it, so a frame followed by none for a while would stay unseen (and a card
blank) until the next. `push` therefore sends a frame again when no newer one has come
within SETTLE: the copy's headers show the frame, and the copy waits unseen in its place.
Every part stays a whole image with its Content-Length, for readers that go by it.

These are helpers for a service's own small asyncio server (research.live is one): it
reads the request line, then either pushes frames or sends one plain response. An image's
address may ask for the light theme with `?theme=light` (`theme`); else it's dark. There's
no framework: GET only, no keep-alive, every response closes its connection. An image may
be read by a page of any origin (CORS): a client's web build fetches the cards to draw them,
and a card shows nothing that isn't in the picture. Pages and redirects get no such header.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from urllib.parse import parse_qs

from chatimage import THEME, THEMES

BOUNDARY = b"frame"
SETTLE = 0.2  # seconds a frame waits for a newer one before it's sent again to be shown
MAX_HEAD = 16 * 1024  # a request's line and headers; the machine's route adds a few
CORS = {"Access-Control-Allow-Origin": "*"}  # on images alone


class BadRequest(Exception):
    pass


async def read_request(reader: asyncio.StreamReader) -> tuple[str, str, str]:
    """The request's method, path and query (without its ?), once its headers are in.
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
    path, _, query = target.partition("?")
    return method, path, query


def theme(query: str) -> str:
    """The theme a request's query asks for (`theme=light`), else the default."""
    asked = parse_qs(query).get("theme", [])
    return asked[-1] if asked and asked[-1] in THEMES else THEME


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
                    **(CORS if content_type.startswith("image/") else {}),
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
    newest frame, until they run out or the client goes; then close. A frame no newer one
    follows within SETTLE is sent again, so that a browser shows it (above). True when
    every frame went out, False when the client left first (the frames are closed either
    way)."""

    def part(image: bytes) -> bytes:
        return b"--%s\r\nContent-Type: %s\r\nContent-Length: %d\r\n\r\n%s\r\n" % (
            BOUNDARY,
            content_type.encode(),
            len(image),
            image,
        )

    following: asyncio.Future[bytes] | None = None
    try:
        writer.write(
            head(
                "200 OK",
                {
                    "Content-Type": f"multipart/x-mixed-replace; boundary={BOUNDARY.decode()}",
                    "Cache-Control": "no-store",
                    "X-Accel-Buffering": "no",
                    **CORS,
                },
            )
        )
        shown: bytes | None = None  # the frame sent last, until a part follows it
        while True:
            following = asyncio.ensure_future(anext(frames))
            if shown is not None:
                done, _ = await asyncio.wait({following}, timeout=SETTLE)
                if not done:
                    writer.write(part(shown))
                    await writer.drain()
            try:
                image = await following
            except StopAsyncIteration:
                break
            writer.write(part(image))
            await writer.drain()
            shown = image
        writer.write(b"--%s--\r\n" % BOUNDARY)
        await writer.drain()
        return True
    except (ConnectionError, OSError):
        return False
    finally:
        if following is not None and not following.done():
            following.cancel()  # which closes a generator waiting for its next frame
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await following
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
