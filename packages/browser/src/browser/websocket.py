"""Just enough of a WebSocket server (RFC 6455) for the take-over view to carry VNC: the
handshake's answer, and frames read from the browser and written to it. noVNC sends and
wants binary messages; text is passed on as bytes too. No extensions, no compression.
"""

import asyncio
import base64
import hashlib
import struct

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 4 << 20  # a client sends keys and pointer moves; this is generous
CONTINUATION, TEXT, BINARY, CLOSE, PING, PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class Closed(Exception):
    """The other side closed the WebSocket, or broke the protocol."""


def accept(key: str) -> str:
    """Sec-WebSocket-Accept for a request's Sec-WebSocket-Key."""
    return base64.b64encode(hashlib.sha1(key.strip().encode() + GUID).digest()).decode()


def handshake(key: str, protocols: str) -> bytes:
    """The 101 answer to an upgrade, taking the `binary` subprotocol when it's offered."""
    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {accept(key)}",
    ]
    if "binary" in [p.strip() for p in protocols.split(",")]:
        lines.append("Sec-WebSocket-Protocol: binary")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def frame(opcode: int, payload: bytes) -> bytes:
    """One unmasked, final frame (a server never masks)."""
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 1 << 16:
        head = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    return head + payload


async def read_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
    """(final, opcode, payload) of the next frame from a client, which must mask it."""
    try:
        b0, b1 = await reader.readexactly(2)
        n = b1 & 0x7F
        if n == 126:
            (n,) = struct.unpack("!H", await reader.readexactly(2))
        elif n == 127:
            (n,) = struct.unpack("!Q", await reader.readexactly(8))
        if not b1 & 0x80:
            raise Closed("a client frame wasn't masked")
        if n > MAX_MESSAGE:
            raise Closed("a frame too big")
        mask = await reader.readexactly(4)
        data = await reader.readexactly(n)
    except asyncio.IncompleteReadError as e:
        raise Closed("the connection ended") from e
    return bool(b0 & 0x80), b0 & 0x0F, unmask(data, mask)


def unmask(data: bytes, mask: bytes) -> bytes:
    """The payload XORed with the repeating mask, all at once as one big integer."""
    n = len(data)
    key = int.from_bytes((mask * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(data, "big") ^ key).to_bytes(n, "big")


async def messages(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Each data message from the client, as bytes, until it closes; pings are answered."""
    parts: list[bytes] = []
    while True:
        final, opcode, payload = await read_frame(reader)
        if opcode == CLOSE:
            writer.write(frame(CLOSE, payload[:2]))
            await writer.drain()
            return
        if opcode == PING:
            writer.write(frame(PONG, payload))
            await writer.drain()
            continue
        if opcode == PONG:
            continue
        if opcode not in (CONTINUATION, TEXT, BINARY):
            raise Closed(f"unknown opcode {opcode}")
        parts.append(payload)
        if sum(map(len, parts)) > MAX_MESSAGE:
            raise Closed("a message too big")
        if final:
            yield b"".join(parts)
            parts = []
