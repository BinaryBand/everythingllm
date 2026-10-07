"""The take-over view: a workspace's browser on your screen, to watch, or to take from the
agent (to log in, get past a CAPTCHA, or steer) and hand back.

It's a page on its own HTTPS port (https://<host>:8454, apps.toml), so its scripts run
on an origin of their own, not the pages site's. A live card links to it through
browser.live, which knows the address: /<token>/, where the token is new with each
container, so a stopped browser's old address goes nowhere.

  GET  /<token>/                 the page: noVNC showing the browser's screen, view-only
                                 while the agent has it; ?tab=<id> brings that tab to the front
  GET  /<token>/app.js, style.css  the page's script and style (static/ beside this file)
  GET  /<token>/novnc/<path>     noVNC's core and vendor files (copied from the browser image
                                 into <data>/novnc by hostctl browser-images)
  GET  /<token>/state            {workspace, control, state, reason, waiting, tabs, approval,
                                 asked, offers, logins, making, made}: `state` what's being done
                                 with it (Runner.activity), logins (and passkeys) and offers
                                 without their secrets
  POST /<token>/take             the user takes the browser
  POST /<token>/give             the user hands it back to the agent
  POST /<token>/approve/<id>, deny/<id>   answer the agent's wish to use a saved login
  POST /<token>/logins           save a login {site, username, password, totp, ask}
  POST /<token>/logins/<id>/delete, logins/<id>/ask {ask}
  POST /<token>/offers/<id>/save {username, ask}, offers/<id>/drop
                                 save, or not, a login the user just sent in the browser
  POST /<token>/passkeys/make {on}  let the browser's pages make a passkey (only while the
                                 user has the browser), saved as the state is next asked for
  GET  /<token>/websockify       the WebSocket noVNC speaks, carried to the container's
                                 x11vnc socket (browser.websocket)
  GET  /login/<id>/              the form for a login the agent asked for (Runner.op_ask_login),
                                 which the request's card links to; login.js, style.css beside it
  GET  /login/<id>/state         {state, site, sites}
  POST /login/<id>/save {site, username, password, totp, ask}, /login/<id>/drop
                                 save the login in the vault, for the request's site or a
                                 parent of it the user picks; or turn the request down
  GET  /health                   ok

A POST or a WebSocket must come from the page's own origin (its Origin header), so no
other page can drive the browser. A login request's form needs no token: its id, long and
known only to its card, is its key, and it can only add a login for the site the agent's
page was on. Nothing here ever sends a password or 2FA secret back:
the page can save and delete logins, not read them. Connections are taken only from loopback or the
server's own address (hostrpc.local_peer), where the machine's HTTPS routes deliver them.
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
from browser.origin import registrable

if TYPE_CHECKING:
    from browser.runner import Runner, Session

HOST = "127.0.0.1"
STATIC = Path(__file__).with_name("static")
MAX_HEAD = 16 * 1024
MAX_BODY = 16 * 1024
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
<p id="problem" class="error" hidden></p>
<section id="approval" class="ask" hidden>
  <span id="approval-text"></span>
  <button id="allow">Allow</button>
  <button id="deny" class="quiet">Don't allow</button>
</section>
<section id="asked"></section>
<section id="offers"></section>
<details id="logins">
  <summary>Saved logins</summary>
  <p class="note">The agent can use these on their own sites, but never read them.</p>
  <ul id="login-list"></ul>
  <form id="add">
    <input name="site" placeholder="Site, like linkedin.com" required autocomplete="off">
    <input name="username" placeholder="Username or email" autocomplete="off">
    <input name="password" type="password" placeholder="Password" autocomplete="new-password">
    <input name="totp" placeholder="2FA secret (optional)" autocomplete="off">
    <label><input name="ask" type="checkbox"> Ask me before each use</label>
    <button>Save login</button>
    <span id="add-error" class="error"></span>
  </form>
  <p id="passkey">
    <button id="make" type="button" hidden>Make a passkey</button>
    <span id="make-note" class="note"></span>
  </p>
</details>
<main id="screen"></main>
<script type="module" src="app.js"></script>
</body>
</html>
"""
LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Log in to {owner}</title>
<link rel="stylesheet" href="style.css">
</head>
<body class="ask-login">
<h1>Log in to <span class="site">{owner}</span></h1>
<p>The agent in <strong>{workspace}</strong> asked for your login for the page it has open,
on <span class="site">{site}</span>, a site of <strong>{owner}</strong>:</p>
<p class="url">{url}</p>
<p class="warning"{new_hidden}>No login in this workspace is for {owner} yet. Make sure that's
the site you mean to log in to before you enter a password: a page can name itself after
another.</p>
<p class="note">It's saved in this workspace's logins. The agent can have the browser fill it
in on the site you pick here, never anywhere else, and can never read it. Only enter the login
you use on that site.</p>
<form id="ask-form"{form_hidden}>
  <label>Site <select name="site">{options}</select></label>
  <input name="username" placeholder="Username or email" autocomplete="off">
  <input name="password" type="password" placeholder="Password" autocomplete="new-password" required>
  <input name="totp" placeholder="2FA secret (optional)" autocomplete="off">
  <label><input name="ask" type="checkbox"> Ask me before each use</label>
  <div class="buttons">
    <button>Save login</button>
    <button type="button" id="decline" class="quiet">Not now</button>
  </div>
</form>
<p id="result"{result_hidden}>{result}</p>
<p id="error" class="error" hidden></p>
<script type="module" src="login.js"></script>
</body>
</html>
"""
ASKED = {
    "saving": "Saving it…",
    "saved": "Saved. Tell the agent in the chat, and it will log in with it.",
    "declined": "You turned this request down.",
    "expired": "This request ran out. Ask the agent to ask again.",
}


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
        self.body: dict = {}

    async def read_body(self, reader: asyncio.StreamReader) -> None:
        """A POST's JSON object, if it has one; live.BadRequest for anything else."""
        try:
            n = int(self.headers.get("content-length") or 0)
        except ValueError:
            raise live.BadRequest("bad Content-Length") from None
        if not n:
            return
        if not 0 < n <= MAX_BODY:
            raise live.BadRequest("body too big")
        try:
            body = json.loads(await asyncio.wait_for(reader.readexactly(n), 10))
        except (asyncio.IncompleteReadError, TimeoutError, ValueError):
            raise live.BadRequest("the body isn't JSON") from None
        if not isinstance(body, dict):
            raise live.BadRequest("the body isn't a JSON object")
        self.body = body

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
        if token == "login":
            return await self.asked(req, reader, writer, what)
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
            try:
                await req.read_body(reader)
                done = await self.post(s, what, req.body)
            except live.BadRequest as e:
                return await live.send(writer, "400 Bad Request", f"{e}\n".encode())
            except hostrpc.RunnerError as e:
                return await self.json(writer, {"error": str(e)}, "400 Bad Request")
            if not done:
                return await live.send(writer, "404 Not Found", b"No such thing.\n")
            return await self.json(writer, await self.state(s))
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
            return await self.json(writer, await self.state(s))
        if what == "websockify":
            return await self.bridge(req, reader, writer, s)
        await live.send(writer, "404 Not Found", b"No such thing.\n")

    async def asked(
        self,
        req: Request,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        what: str,
    ) -> None:
        """A login request's form, its files, its state and its POSTs."""
        r = self.runner
        request, slash, rest = what.partition("/")
        asked = r.asked_by_id(request) if request.startswith("lr-") else None
        if asked is None:
            return await live.send(
                writer,
                "404 Not Found",
                b"This login request isn't known here (the browser service restarted "
                b"since, or it's over a day old).\n",
            )
        if not slash:  # the page's relative links need the slash
            return await live.send(
                writer, "302 Found", b"", headers={"Location": f"/login/{request}/"}
            )
        if req.method == "POST":
            if not req.same_origin():
                return await live.send(writer, "403 Forbidden", b"Not from the page.\n")
            try:
                await req.read_body(reader)
                body = req.body
                if rest == "save":
                    await r.fulfil(
                        asked, text(body, "site"), text(body, "username"),
                        text(body, "password"), text(body, "totp"), body.get("ask") is True,
                    )  # fmt: skip
                elif rest == "drop":
                    r.decline(asked)
                else:
                    return await live.send(writer, "404 Not Found", b"No such thing.\n")
            except live.BadRequest as e:
                return await live.send(writer, "400 Bad Request", f"{e}\n".encode())
            except hostrpc.RunnerError as e:
                return await self.json(writer, {"error": str(e)}, "400 Bad Request")
            return await self.json(writer, self.asked_state(asked))
        if req.method != "GET":
            return await live.send(writer, "405 Method Not Allowed", b"GET or POST.\n")
        if rest == "":
            state = r.asked_state(asked)
            options = "".join(
                f'<option value="{html.escape(site)}">{html.escape(site)}</option>'
                for site in asked.sites
            )
            owner = registrable(asked.site)
            try:
                logins = await asyncio.to_thread(r.vault.logins, asked.workspace)
            except hostrpc.RunnerError:
                logins = []
            known = any(registrable(x.get("site", "")) == owner for x in logins)
            body = LOGIN_PAGE.format(
                site=html.escape(asked.site),
                owner=html.escape(owner),
                new_hidden=" hidden" if known else "",
                workspace=html.escape(asked.workspace),
                url=html.escape(asked.url),
                options=options,
                form_hidden="" if state == "waiting" else " hidden",
                result_hidden=" hidden" if state == "waiting" else "",
                result=html.escape(ASKED.get(state, "")),
            ).encode()
            return await self.send(writer, body, "text/html; charset=utf-8")
        if rest in ("login.js", "style.css"):
            return await self.file(writer, STATIC / rest)
        if rest == "state":
            return await self.json(writer, self.asked_state(asked))
        await live.send(writer, "404 Not Found", b"No such thing.\n")

    def asked_state(self, asked) -> dict:
        state = self.runner.asked_state(asked)
        return {
            "state": state,
            "site": asked.site,
            "sites": asked.sites,
            "message": ASKED.get(state, ""),
        }

    async def post(self, s: Session, what: str, body: dict) -> bool:
        """Do what a POST asks; False for a path that isn't one."""
        r, ws = self.runner, s.workspace
        parts = what.split("/")
        match parts:
            case ["take"]:
                await r.take(s)
            case ["give"]:
                await r.give_back(s)
            case ["approve" | "deny" as answer, approval]:
                r.answer(s, approval, answer == "approve")
            case ["logins"]:
                await asyncio.to_thread(
                    r.vault.add, ws, text(body, "site"), text(body, "username"),
                    text(body, "password"), text(body, "totp"), body.get("ask") is True,
                )  # fmt: skip
            case ["logins", login, "delete"]:
                await asyncio.to_thread(r.vault.delete, ws, login)
            case ["logins", login, "ask"]:
                await asyncio.to_thread(
                    r.vault.update, ws, login, ask=body.get("ask") is True
                )
            case ["offers", offer, "save"]:
                name = body.get("username")
                await r.save_offer(
                    s,
                    offer,
                    name if isinstance(name, str) else None,
                    body.get("ask") is True,
                )
            case ["offers", offer, "drop"]:
                await r.call(s, "drop_offer", {"id": offer})
            case ["passkeys", "make"]:
                await r.make_passkeys(s, body.get("on") is True)
            case _:
                return False
        return True

    async def state(self, s: Session) -> dict:
        tabs = [
            {"id": t.id, "title": t.title, "url": t.url}
            for t in self.runner.tabs.values()
            if t.workspace == s.workspace and t.open
        ]
        if s.making:  # save what was made, and see whether they still can
            await self.runner.save_made(s)
        approval = s.approval
        asked = [
            {"id": a.id, "site": a.site, "link": f"/login/{a.id}/"}
            for a in self.runner.asked.values()
            if a.workspace == s.workspace and self.runner.waiting(a)
        ]
        try:
            logins = await asyncio.to_thread(self.runner.vault.logins, s.workspace)
        except hostrpc.RunnerError as e:
            logins = [{"error": str(e)}]
        return {
            "workspace": s.workspace,
            "control": s.control,
            "state": self.runner.activity(s),
            "reason": s.reason,
            "waiting": s.asked and s.control == "user",
            "tabs": tabs,
            "approval": {
                "id": approval.id,
                "kind": approval.kind,
                "site": approval.site,
                "username": approval.username,
                "url": approval.url,
            }
            if approval is not None
            else None,
            "asked": asked,
            "offers": await self.runner.offers(s),
            "logins": logins,
            "making": s.making,
            "made": s.made,
        }

    async def send(
        self,
        writer: asyncio.StreamWriter,
        body: bytes,
        kind: str,
        status: str = "200 OK",
    ) -> None:
        await live.send(
            writer,
            status,
            body,
            kind,
            headers={
                "Content-Security-Policy": CSP,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            },
        )

    async def json(
        self, writer: asyncio.StreamWriter, data: dict, status: str = "200 OK"
    ) -> None:
        await self.send(writer, json.dumps(data).encode(), "application/json", status)

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


def text(body: dict, key: str) -> str:
    value = body.get(key)
    return value if isinstance(value, str) else ""
