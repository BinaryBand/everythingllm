"""The take-over view: a workspace's browser on your screen, to watch, or to take from the
agent (to log in, get past a CAPTCHA, or steer) and hand back.

It's a page on its own tailnet port (https://<host>:8454, apps.toml), so its scripts run
on an origin of their own, not the pages site's. A live card links to it through
browser.live, which knows the address: /<token>/, where the token is new with each
container, so a stopped browser's old address goes nowhere.

  GET  /<token>/                 the page: noVNC showing the browser's screen, view-only
                                 while the agent has it; ?tab=<id> brings that tab to the front
  GET  /<token>/app.js, style.css  the page's script and style (static/ beside this file)
  GET  /<token>/novnc/<path>     noVNC's core and vendor files (copied from the browser image
                                 into <data>/novnc by hostctl browser-images)
  GET  /<token>/state            {workspace, control, reason, tabs}
  POST /<token>/take             the user takes the browser
  POST /<token>/give             the user hands it back to the agent
  GET  /<token>/websockify       the WebSocket noVNC speaks, carried to the container's
                                 x11vnc socket (browser.websocket)
  GET  /health                   ok

A POST or a WebSocket must come from the page's own origin (its Origin header), so no
other page can drive the browser. Connections are taken only from loopback or the
server's own address (hostrpc.local_peer), where tailscale serve delivers them.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import hostrpc
from chatimage import live

from browser import websocket

if TYPE_CHECKING:
    from browser.runner import Runner, Session

HOST = "127.0.0.1"
STATIC = Path(__file__).with_name("static")
MAX_HEAD = 16 * 1024
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Browser · {workspace}</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<header>
  <strong>Browser · {workspace}</strong>
  <span id="state">Connecting…</span>
  <button id="take" hidden>Take over</button>
  <button id="give" hidden>Hand back to the agent</button>
</header>
<p id="reason" hidden></p>
<main id="screen"></main>
<script type="module" src="app.js"></script>
</body>
</html>
"""


@contextlib.contextmanager
def watching(s: Session):
    s.viewers += 1
    try:
        yield
    finally:
        s.viewers -= 1


class Request:
    def __init__(self, method: str, target: str, headers: dict[str, str]):
        parts = urlsplit(target)
        self.method, self.path, self.headers = method, parts.path, headers
        self.query = {k: v[0] for k, v in parse_qs(parts.query).items()}

    def same_origin(self) -> bool:
        """Whether Origin names this server, as the Host header does."""
        origin = self.headers.get("origin", "")
        return bool(origin) and urlsplit(origin).netloc == self.headers.get("host", "")


async def read_request(reader: asyncio.StreamReader) -> Request:
    """The request line and headers; live.BadRequest for anything else."""
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as e:
        raise live.BadRequest("incomplete request") from e
    if len(head) > MAX_HEAD:
        raise live.BadRequest("request too big")
    line, *rest = head.decode("latin-1").split("\r\n")
    parts = line.split()
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise live.BadRequest("not an HTTP/1 request")
    headers = {}
    for h in rest:
        key, sep, value = h.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    return Request(parts[0], parts[1], headers)


class Takeover:
    def __init__(self, runner: Runner):
        self.runner = runner

    @property
    def novnc(self) -> Path:
        return self.runner.config.data / "novnc"

    async def serve(self, port: int) -> asyncio.Server:
        return await asyncio.start_server(
            self.handle, HOST, port, limit=MAX_HEAD + 1024
        )

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            req = await asyncio.wait_for(read_request(reader), 10)
        except (live.BadRequest, TimeoutError):
            return await live.send(writer, "400 Bad Request", b"Bad request.\n")
        if not hostrpc.local_peer(
            writer.get_extra_info("peername"), writer.get_extra_info("sockname")
        ):
            return await live.send(writer, "403 Forbidden", b"Not from here.\n")
        try:
            await self.route(req, reader, writer)
        except Exception:
            self.runner.log.exception(
                "take-over view: %s %s failed", req.method, req.path
            )
            if not writer.is_closing():
                await live.send(
                    writer, "500 Internal Server Error", b"Something went wrong.\n"
                )

    async def route(
        self, req: Request, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if req.path == "/health":
            return await live.send(writer, "200 OK", b"ok\n")
        _, token, *rest = req.path.split("/", 2) + [""]
        what = rest[0]
        s = self.runner.by_token(token) if token else None
        if s is None:
            return await live.send(
                writer,
                "404 Not Found",
                b"This browser isn't running (or the address is old).\n",
            )
        if req.path == f"/{token}":  # the page's relative links need the slash
            return await live.send(
                writer, "302 Found", b"", headers={"Location": f"/{token}/"}
            )
        if req.method == "POST":
            if not req.same_origin():
                return await live.send(writer, "403 Forbidden", b"Not from the page.\n")
            if what == "take":
                self.runner.take(s)
            elif what == "give":
                self.runner.give_back(s)
            else:
                return await live.send(writer, "404 Not Found", b"No such thing.\n")
            return await self.json(writer, self.state(s))
        if req.method != "GET":
            return await live.send(writer, "405 Method Not Allowed", b"GET or POST.\n")
        if what == "":
            tab = self.runner.tabs.get(req.query.get("tab", ""))
            if tab is not None and tab.workspace == s.workspace and tab.open:
                with contextlib.suppress(hostrpc.RunnerError):
                    await self.runner.call(s, "front", {"thread": tab.thread})
            body = PAGE.format(workspace=html.escape(s.workspace)).encode()
            return await self.send(writer, body, "text/html; charset=utf-8")
        if what in ("app.js", "style.css"):
            return await self.file(writer, STATIC / what)
        if what.startswith("novnc/"):
            return await self.file(
                writer, self.novnc / what.removeprefix("novnc/"), self.novnc
            )
        if what == "state":
            return await self.json(writer, self.state(s))
        if what == "websockify":
            return await self.bridge(req, reader, writer, s)
        await live.send(writer, "404 Not Found", b"No such thing.\n")

    def state(self, s: Session) -> dict:
        tabs = [
            {"id": t.id, "title": t.title, "url": t.url}
            for t in self.runner.tabs.values()
            if t.workspace == s.workspace and t.open
        ]
        return {
            "workspace": s.workspace,
            "control": s.control,
            "reason": s.reason,
            "waiting": s.asked and s.control == "user",
            "tabs": tabs,
        }

    async def send(self, writer: asyncio.StreamWriter, body: bytes, kind: str) -> None:
        await live.send(
            writer,
            "200 OK",
            body,
            kind,
            headers={
                "Content-Security-Policy": CSP,
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def json(self, writer: asyncio.StreamWriter, data: dict) -> None:
        await self.send(writer, json.dumps(data).encode(), "application/json")

    async def file(
        self, writer: asyncio.StreamWriter, path: Path, root: Path | None = None
    ) -> None:
        """A plain file under `root` (or the static folder), by its suffix's type."""
        root = (root or STATIC).resolve()
        try:
            target = path.resolve()
        except OSError:
            target = root
        kind = TYPES.get(target.suffix)
        if (
            kind is None
            or ".." in path.parts
            or not target.is_relative_to(root)
            or not target.is_file()
        ):
            return await live.send(writer, "404 Not Found", b"No such file.\n")
        await self.send(writer, await asyncio.to_thread(target.read_bytes), kind)

    async def bridge(
        self,
        req: Request,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        s: Session,
    ) -> None:
        """Carry the WebSocket to the container's VNC socket and back, until either ends."""
        key = req.headers.get("sec-websocket-key", "")
        if req.headers.get("upgrade", "").lower() != "websocket" or not key:
            return await live.send(writer, "400 Bad Request", b"A WebSocket only.\n")
        if not req.same_origin():
            return await live.send(writer, "403 Forbidden", b"Not from the page.\n")
        try:
            vnc_reader, vnc_writer = await asyncio.open_unix_connection(str(s.vnc))
        except OSError:
            return await live.send(
                writer, "502 Bad Gateway", b"The browser's screen isn't there.\n"
            )
        writer.write(
            websocket.handshake(key, req.headers.get("sec-websocket-protocol", ""))
        )
        await writer.drain()

        async def up() -> None:
            async for message in websocket.messages(reader, writer):
                vnc_writer.write(message)
                await vnc_writer.drain()

        async def down() -> None:
            while chunk := await vnc_reader.read(65536):
                writer.write(websocket.frame(websocket.BINARY, chunk))
                await writer.drain()

        with watching(s):
            tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                vnc_writer.close()
                with contextlib.suppress(ConnectionError, OSError):
                    writer.write(websocket.frame(websocket.CLOSE, b"\x03\xe8"))
                await live.close(writer)
                s.used = self.runner.now()
