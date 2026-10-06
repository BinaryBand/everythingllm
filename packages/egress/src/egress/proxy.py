"""egress-proxy: the service containers' only way out of egress-net, an internal podman
network with no route anywhere else. It runs in its own container, on egress-net and on
podman's default network (host/quadlet/egress-proxy.container.in).

Each container's HTTPS_PROXY and HTTP_PROXY point here, at egress.toml's `port`, and its
EGRESS_PROXY (publicweb.public_client's fetches of URLs from the web) at `public_port`. A
connection is judged by its source address, which names the container and so its profile
(egress.config, egress.toml), by the port it came in on, and by the host and port it asks
for:

- `CONNECT host:port` opens a tunnel (https); an absolute-form `GET http://host/...` is
  sent on, with its head rewritten to the origin form and `Connection: close`.
- A host in the profile's `allow` exceptions is resolved and connected to whatever it is
  (the tailnet's AnythingLLM and SearXNG, PyPI), but never on the public port. Otherwise,
  for a `public` profile on port
  80 or 443, the host must resolve to public addresses only: publicweb.public_address,
  the same rule the services apply on the host. Either way the name is resolved once, here,
  and the connection goes to that address, so a name can't answer differently in between.
- Anything else, and any connection from an address no profile has, is refused (403).

Each connection logs its profile, method, host:port and verdict, never a path or a query.

Config (environment):
  EGRESS_LISTEN  the address to listen on (default the proxy's address in egress.toml,
                 so containers on podman's default network can't reach it)
  and egress.config's placeholders (PUBLIC_HOST, NTFY_HOST)
"""

import asyncio
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import urlsplit

from publicweb import public_address

from egress.config import Config, host_port, load, normal_host

log = logging.getLogger("egress")

HEAD_LIMIT = 64 * 1024  # a request's head, at most
HEAD_SECONDS = 30
CONNECT_SECONDS = 10
IDLE_SECONDS = 3600  # a tunnel with nothing either way for this long is closed
MAX_CONNECTIONS = 512  # at once; uv alone opens dozens on a first sync
# Headers that are about the connection to the proxy, not the request: not passed on.
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-connection",
    "proxy-authorization",
    "proxy-authenticate",
    "te",
    "trailer",
    "upgrade",
}

Opener = Callable[
    [str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
]


class BadRequest(Exception):
    pass


class Refused(Exception):
    pass


@dataclass(frozen=True)
class Request:
    method: str
    host: str  # normal_host'd
    port: int
    forward: bytes = b""  # plain http: the head to send on; CONNECT: nothing

    @property
    def where(self) -> str:
        return (
            f"[{self.host}]:{self.port}"
            if ":" in self.host
            else f"{self.host}:{self.port}"
        )


def parse(head: bytes) -> Request:
    """A request's head (up to and with its blank line) as the proxy acts on it."""
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise BadRequest("not an HTTP/1 request line")
    method, target, _ = parts
    if method == "CONNECT":
        try:
            host, port = host_port(target)
        except ValueError:
            raise BadRequest("CONNECT wants host:port") from None
        return Request(method, host, port)
    url = urlsplit(target)
    try:
        port = url.port or 80
    except ValueError:
        raise BadRequest("a bad port") from None
    if url.scheme != "http" or not url.hostname:
        raise BadRequest("only CONNECT, or an absolute http:// URL")
    host = normal_host(url.hostname)
    headers = []
    for line in lines[1:]:
        if not line:
            break
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip() or line[0] in " \t":
            raise BadRequest("a malformed header")
        headers.append((name, value.strip()))
    named = {
        token.strip().lower()
        for name, value in headers
        if name.lower() == "connection"
        for token in value.split(",")
    }
    authority = f"[{host}]" if ":" in host else host
    if port != 80:
        authority += f":{port}"
    path = (url.path or "/") + (f"?{url.query}" if url.query else "")
    kept = [
        f"{name}: {value}"
        for name, value in headers
        if name.lower() not in HOP_BY_HOP | named | {"host"}
    ]
    forward = "\r\n".join(
        [f"{method} {path} HTTP/1.1", f"Host: {authority}", *kept, "Connection: close"]
    )
    return Request(method, host, port, (forward + "\r\n\r\n").encode("latin-1"))


def resolve(host: str, port: int) -> str:
    """The first address `host` resolves to, whatever it is: for allow-list exceptions."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, ValueError) as e:
        raise Refused(f"can't resolve {host}: {e}") from None
    return str(infos[0][4][0])


async def reply(writer: asyncio.StreamWriter, status: str, text: str) -> None:
    body = f"{text}\n".encode()
    writer.write(
        f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        + body
    )
    try:
        await writer.drain()
    except ConnectionError:
        pass


class Tunnel:
    """When a tunnel last moved, either way: a download whose request went up an hour ago
    keeps the way up open, and an upload the server hasn't answered yet the way down."""

    def __init__(self) -> None:
        self.moved = time.monotonic()

    def left(self) -> float:
        """Seconds until the tunnel has been quiet both ways for IDLE_SECONDS."""
        return self.moved + IDLE_SECONDS - time.monotonic()


async def pipe(
    src: asyncio.StreamReader, dst: asyncio.StreamWriter, tunnel: Tunnel
) -> None:
    """Copy until `src` ends, then end `dst`'s side; or stop, leaving it, once the tunnel
    has been quiet both ways for IDLE_SECONDS."""
    try:
        while (left := tunnel.left()) > 0:
            try:
                data = await asyncio.wait_for(src.read(65536), left)
            except TimeoutError:
                continue  # the other way may have moved meanwhile
            if not data:
                if dst.can_write_eof():
                    dst.write_eof()
                return
            tunnel.moved = time.monotonic()
            dst.write(data)
            await dst.drain()
    except OSError:
        pass


class Proxy:
    def __init__(
        self,
        config: Config,
        opener: Opener = asyncio.open_connection,
        public_only: bool = False,
    ):
        self.config = config
        self.opener = opener  # tests connect elsewhere than the address checked
        self.public_only = public_only  # the public port: no allow exceptions
        self.active = 0

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        client = str(peer[0]) if peer else ""
        if self.active >= MAX_CONNECTIONS:
            log.warning("%s: refused, %d connections already", client, self.active)
            await reply(writer, "503 Service Unavailable", "Too many connections.")
            writer.close()
            return
        self.active += 1
        try:
            await self.serve(client, reader, writer)
        except Exception:  # one connection's trouble mustn't reach the others
            log.exception("%s: failed", client)
        finally:
            self.active -= 1
            writer.close()

    async def serve(
        self,
        client: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_SECONDS)
            request = parse(head)
        except (asyncio.IncompleteReadError, TimeoutError, ConnectionError):
            return
        except (asyncio.LimitOverrunError, BadRequest) as e:
            why = str(e) if isinstance(e, BadRequest) else "a head that's too long"
            log.info("%s: bad request: %s", client, why)
            return await reply(writer, "400 Bad Request", f"Bad request: {why}.")
        profile = self.config.profile_for(client)
        who = profile.name if profile else client
        try:
            if profile is None:
                raise Refused("not a known container on egress-net")
            how = profile.judge(request.host, request.port, self.public_only)
            if how is None:
                raise Refused(
                    f"{request.where} isn't allowed for {profile.name}"
                    + (" (public hosts on ports 80 and 443)" if profile.public else "")
                    + (" on the public port" if self.public_only else "")
                )
            if how == "allow":
                address = await asyncio.to_thread(resolve, request.host, request.port)
            else:
                address = await asyncio.to_thread(
                    public_address, request.host, request.port, Refused
                )
        except Refused as e:
            log.info("%s %s %s refused: %s", who, request.method, request.where, e)
            return await reply(writer, "403 Forbidden", f"Refused: {e}.")
        try:
            up_reader, up_writer = await asyncio.wait_for(
                self.opener(address, request.port), CONNECT_SECONDS
            )
        except (OSError, TimeoutError) as e:
            log.info(
                "%s %s %s unreachable: %s",
                who,
                request.method,
                request.where,
                e or "timeout",
            )
            return await reply(
                writer, "502 Bad Gateway", f"Couldn't reach {request.where}."
            )
        log.info("%s %s %s allowed (%s)", who, request.method, request.where, how)
        try:
            if request.method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            else:
                up_writer.write(request.forward)
            # The answer coming back is what counts: once it ends, so does the connection.
            tunnel = Tunnel()
            up = asyncio.ensure_future(pipe(reader, up_writer, tunnel))
            await pipe(up_reader, writer, tunnel)
            up.cancel()
            with suppress(asyncio.CancelledError):
                await up
        except ConnectionError:
            pass
        finally:
            up_writer.close()


async def serve(
    config: Config, host: str, port: int | None = None, public_only: bool = False
) -> asyncio.Server:
    proxy = Proxy(config, public_only=public_only)
    if port is None:
        port = config.public_port if public_only else config.port
    return await asyncio.start_server(proxy.handle, host, port, limit=HEAD_LIMIT)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load()
    host = os.environ.get("EGRESS_LISTEN") or config.proxy

    async def run() -> None:
        server = await serve(config, host)
        public = await serve(config, host, public_only=True)
        log.info(
            "egress-proxy on %s:%d (and :%d, public hosts only) for %s",
            host,
            config.port,
            config.public_port,
            ", ".join(f"{c} ({ip})" for c, ip in config.ips().items()),
        )
        async with server, public:
            await asyncio.gather(server.serve_forever(), public.serve_forever())

    asyncio.run(run())


if __name__ == "__main__":
    main()
