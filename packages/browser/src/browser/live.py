"""The browser's live cards: each chat thread's tab as a picture in the chat that keeps
showing what the page looks like as the agent (or the user) works in it.

The skills hand the agent a card line for the tab (Runner.card):

    [![Browser: <title>](https://<host>:8445/_live/browser/<id>.jpg)](https://<host>:8445/_live/browser/<id>)

The machine routes https://<host>:8445/_live/browser to this server's port (apps.toml),
stripping the prefix or not, so paths are taken with or without it.

- `<id>.jpg` is the card: a screenshot of the tab under a strip saying what's being done
  with it (the agent at work or idle, waiting for you, yours, closed: Runner.state) and
  what was done last, pushed again (chatimage.live, as JPEG) whenever it changes,
  checked every GAP seconds, until MAX_STREAM passes. A card being watched keeps the
  browser from being stopped as idle. A closed tab shows its last screenshot, dimmed, and
  the stream waits for it to open again. A tab the runner doesn't know (from before a
  restart) gets one frame saying so. `?theme=light` draws it in the light theme
  (chatimage.live.theme), as it does the login request's card.
- `<id>` is where the card links: the take-over view of the tab's browser while it runs
  (a redirect to browser.takeover, whose address changes with each container), else a
  page saying it's closed.

The agent's request for a login (Runner.op_ask_login) has a card too:

    [![Log in to <site>](https://<host>:8445/_live/browser/login/<id>.png)](https://<host>:8445/_live/browser/login/<id>)

- `login/<id>.png` says what the request is for and how it stands (waiting, saved, declined
  or run out), pushed again when that changes.
- `login/<id>` is where it links: the request's form in the take-over view (a redirect,
  /login/<id>/ there), or a page saying it isn't known.

A tab's id is `bw-` and 16 hex digits, not guessable, and a request's `lr-` and 32: the
card and its link are the only way to either, but for a client with an AnythingLLM
developer API key, which `chat/<workspace>/<thread>` (or `chat/<workspace>` for its main
chat) tells a chat's cards and how they stand, and whose `card.jpg` is the tab's card as it
is now, readable from any origin (browser.chats). A connection from anywhere but loopback or the server's own address
is refused (hostrpc.local_peer).

Config (environment):
  LIVE_HOST  the address to listen on (default 127.0.0.1)
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import io
import json
import os
import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import hostrpc
from chatimage import (
    EDGE,
    THEME,
    THEMES,
    accent_for,
    clean,
    fit,
    font,
    live,
    progress,
)
from PIL import Image, ImageDraw, ImageEnhance

from browser import chats
from browser.origin import registrable

if TYPE_CHECKING:
    from browser.runner import Runner
    from browser.tabs import LoginRequest, Tab

HOST = "127.0.0.1"
CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
WIDTH = 1280  # the screen's width; the chat shows it at up to 800
STRIP = 132  # the strip above the screenshot
STATES = {
    "working": "the agent is browsing",
    "idle": "the agent's, idle",
    "waiting": "waiting for you",
    "user": "you have it",
    "closed": "closed",
}


def picture(
    shot: bytes,
    workspace: str,
    state: str,
    title: str,
    url: str,
    last: str,
    theme: str = THEME,
) -> bytes:
    """A frame as a JPEG: the strip (what's being done with the tab, the page's title, its
    address and what was done last) above the screenshot, dimmed once the tab is closed."""
    p = THEMES[theme]
    accent = {"user": p.user, "waiting": p.user, "closed": p.faint}.get(
        state, accent_for("Browser", p)
    )
    try:
        screen = Image.open(io.BytesIO(shot)).convert("RGB") if shot else None
    except OSError:
        screen = None
    if screen is not None and screen.width != WIDTH:
        screen = screen.resize((WIDTH, round(screen.height * WIDTH / screen.width)))
    height = screen.height if screen is not None else 360
    image = Image.new("RGB", (WIDTH, STRIP + height), p.panel)
    d = ImageDraw.Draw(image)
    d.rectangle((0, 0, WIDTH - 1, STRIP + height - 1), outline=p.line, width=EDGE)
    d.rectangle((0, 0, 10, STRIP), fill=accent)
    x, width = 36, WIDTH - 72
    small, big = font("regular", 26), font("bold", 36)
    label = clean(f"Browser · {workspace} · {STATES.get(state, state)}")
    d.text((x, 16), fit(d, label, small, width), font=small, fill=accent)
    d.text(
        (x, 50),
        fit(d, clean(title) or clean(url) or "A new tab", big, width),
        font=big,
        fill=p.title,
    )
    line = " · ".join(t for t in (clean(url), clean(last)) if t)
    d.text((x, 96), fit(d, line, small, width), font=small, fill=p.text)
    if screen is not None:
        if state == "closed":
            screen = ImageEnhance.Brightness(screen).enhance(0.4)
        image.paste(screen, (0, STRIP))
    else:
        note = "Nothing to show yet" if state != "closed" else "The browser is closed"
        d.text((x, STRIP + 150), note, font=big, fill=p.faint)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=72, optimize=True)
    return out.getvalue()


class Live:
    PATH = "/_live/browser/"
    GAP = 1.0  # seconds between looks at an open tab
    TICK = 30.0  # seconds between frames at most, the same one again if nothing changed
    MAX_STREAM = 30 * 60  # seconds one connection is pushed frames; a reload asks again
    ROUTE = re.compile(r"(?:/_live/browser)?/(bw-[0-9a-f]{16})(\.jpg)?")
    ASK_ROUTE = re.compile(r"(?:/_live/browser)?/login/(lr-[0-9a-f]{32})(\.png)?")

    def __init__(self, runner: Runner, chat: chats.Chats | None = None):
        self.runner = runner
        self.chats = chat or chats.Chats(runner)

    async def serve(self, port: int) -> asyncio.Server:
        return await asyncio.start_server(
            self.handle, os.environ.get("LIVE_HOST") or HOST, port
        )

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            method, path, query, headers = await asyncio.wait_for(
                live.read_head(reader), 10
            )
        except (live.BadRequest, TimeoutError):
            return await live.send(writer, "400 Bad Request", b"Bad request.\n")
        if not hostrpc.local_peer(
            writer.get_extra_info("peername"), writer.get_extra_info("sockname")
        ):
            return await live.send(writer, "403 Forbidden", b"Not from here.\n")
        if chat := chats.ROUTE.fullmatch(path):
            return await self.chat(
                writer, method, chat[1], chat[2], headers, bool(chat[3]), query
            )
        if method != "GET":
            return await live.send(writer, "405 Method Not Allowed", b"GET only.\n")
        theme = live.theme(query)
        if asked := self.ASK_ROUTE.fullmatch(path):
            try:
                return await self.asked(reader, writer, asked[1], bool(asked[2]), theme)
            except Exception:
                self.runner.log.exception("login request card %s failed", asked[1])
                if not writer.is_closing():
                    await live.send(
                        writer, "500 Internal Server Error", b"Something went wrong.\n"
                    )
                return
        route = self.ROUTE.fullmatch(path)
        if not route:
            return await live.send(writer, "404 Not Found", b"No such tab.\n")
        tab = self.runner.tabs.get(route[1])
        try:
            if route[2] and tab is not None:
                await self.stream(reader, writer, tab, theme)
            elif route[2]:
                frame = await asyncio.to_thread(
                    progress.draw,
                    "This browser tab isn't open here",
                    "Browser",
                    None,
                    "It was open before the browser service restarted.",
                    "interrupted",
                    theme,
                )
                await live.send(writer, "200 OK", frame, "image/png")
            else:
                await self.page(writer, tab)
        except Exception:  # one viewer's trouble mustn't reach the browsers
            self.runner.log.exception("live card for %s failed", route[1])
            if not writer.is_closing():
                await live.send(
                    writer, "500 Internal Server Error", b"Something went wrong.\n"
                )

    async def chat(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        workspace: str,
        thread: str | None,
        headers: dict[str, str],
        card: bool = False,
        query: str = "",
    ) -> None:
        """A chat's cards and how they stand, or (`card`) its tab's card as it is now, for
        a client with a key (browser.chats)."""
        # A web client's preflight, for the Authorization header.
        if method == "OPTIONS":
            return await live.send(writer, "204 No Content", headers=chats.CORS)
        if method != "GET":
            return await live.send(
                writer, "405 Method Not Allowed", b"GET only.\n", headers=chats.CORS
            )
        try:
            if card:
                tab = await self.chats.tab(workspace, thread, headers)
                shot = await self.runner.screenshot(tab, self.GAP)
                frame = await self.draw(
                    tab, shot, self.runner.state(tab), live.theme(query)
                )
                return await live.send(
                    writer, "200 OK", frame, "image/jpeg", headers=chats.CORS
                )
            status, body = await self.chats.answer(workspace, thread, headers)
        except chats.Refused as e:
            status, body = e.status, {"error": e.error}
        except Exception:
            self.runner.log.exception("a chat's browser for a client failed")
            status, body = (
                "500 Internal Server Error",
                {"error": "Something went wrong."},
            )
        await live.send(
            writer,
            status,
            json.dumps(body).encode(),
            "application/json",
            headers=chats.CORS,
        )

    async def stream(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        tab: Tab,
        theme: str,
    ) -> None:
        """Push the tab's frames until the viewer goes: a viewer sends nothing after its
        request, so the end of what it sends is its leaving, noticed at once rather than
        at the next frame, which for a page that doesn't move may be TICK away."""
        gone = asyncio.Event()

        async def watch() -> None:
            with contextlib.suppress(ConnectionError, OSError):
                while await reader.read(1024):
                    pass
            gone.set()

        watcher = asyncio.create_task(watch())
        try:
            # No CORS: the tab may be logged in somewhere, and a page that knew the card's
            # address could read its screenshots. A web client with a key has the chat's
            # card.jpg (browser.chats).
            await live.push(
                writer, self.frames(tab, gone, theme), "image/jpeg", cors=False
            )
        finally:
            watcher.cancel()

    async def frames(
        self, tab: Tab, gone: asyncio.Event | None = None, theme: str = THEME
    ) -> AsyncIterator[bytes]:
        """A frame now, then one whenever the tab looks different, until MAX_STREAM or the
        viewer is `gone`."""
        gone = gone or asyncio.Event()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.MAX_STREAM
        shown, sent = None, 0.0
        tab.viewers += 1
        try:
            while True:
                shot = await self.runner.screenshot(tab, self.GAP)
                state = self.runner.state(tab)
                now = (hash(shot), state, tab.title, tab.url, tab.last)
                if now != shown or loop.time() - sent >= self.TICK:
                    shown, sent = now, loop.time()
                    yield await self.draw(tab, shot, state, theme)
                left = deadline - loop.time()
                if left <= 0 or gone.is_set():
                    return
                tab.changed.clear()
                # An open tab is looked at again in a second (pages move on their own); a
                # closed one waits until it's used again.
                wait = self.GAP if state != "closed" else self.TICK
                waits = [
                    asyncio.ensure_future(tab.changed.wait()),
                    asyncio.ensure_future(gone.wait()),
                ]
                try:
                    await asyncio.wait(
                        waits,
                        timeout=min(wait, left),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for w in waits:
                        w.cancel()
                if gone.is_set():
                    return
        finally:
            tab.viewers -= 1

    async def draw(self, tab: Tab, shot: bytes, state: str, theme: str) -> bytes:
        """The tab's frame from its screenshot (picture), drawn off the loop."""
        return await asyncio.to_thread(
            picture, shot, tab.workspace, state, tab.title, tab.url, tab.last, theme
        )

    async def asked(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request: str,
        image: bool,
        theme: str,
    ) -> None:
        """A login request's card, or (not `image`) where it links."""
        req = self.runner.asked_by_id(request)
        if image and req is None:
            frame = await asyncio.to_thread(
                progress.draw,
                "This login request isn't known here",
                "Browser · login",
                None,
                "The browser service restarted since, or it's a day old; the agent can ask again.",
                "interrupted",
                theme,
            )
            return await live.send(writer, "200 OK", frame, "image/png")
        if image:
            gone = asyncio.Event()

            async def watch() -> None:
                with contextlib.suppress(ConnectionError, OSError):
                    while await reader.read(1024):
                        pass
                gone.set()

            watcher = asyncio.create_task(watch())
            try:
                await live.push(
                    writer, self.asked_frames(req, gone, theme), "image/png"
                )
            finally:
                watcher.cancel()
            return
        if req is not None:
            location = self.runner.login_form(req)
            return await live.send(
                writer, "302 Found", b"", headers={"Location": location}
            )
        await self.note(
            writer,
            "Log in",
            "This login request isn't known here (the browser service restarted since, "
            "or it's over a day old). Ask the agent to ask again.",
        )

    async def asked_frames(
        self, req: LoginRequest, gone: asyncio.Event, theme: str
    ) -> AsyncIterator[bytes]:
        """A frame now and one each time the request's state changes, until it's no longer
        waiting, MAX_STREAM passes or the viewer is `gone`."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.MAX_STREAM
        while True:
            state = self.runner.asked_state(req)
            yield await asyncio.to_thread(asked_picture, req, state, theme)
            left = min(
                deadline - loop.time(),
                self.runner.asked_left(req),  # when it runs out
            )
            if state not in ("waiting", "saving") or left <= 0 or gone.is_set():
                return
            req.changed.clear()
            waits = [
                asyncio.ensure_future(req.changed.wait()),
                asyncio.ensure_future(gone.wait()),
            ]
            try:
                await asyncio.wait(
                    waits, timeout=left + 0.1, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for w in waits:
                    w.cancel()
            if gone.is_set():
                return

    async def note(self, writer: asyncio.StreamWriter, title: str, what: str) -> None:
        body = (
            "<!doctype html><html lang='en'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title>"
            "<body style='font:16px/1.5 system-ui,sans-serif;max-width:40em;margin:3em auto;padding:0 1em'>"
            f"<h1>{html.escape(title)}</h1><p>{html.escape(what)}</p></body></html>"
        ).encode()
        await live.send(
            writer,
            "200 OK",
            body,
            "text/html; charset=utf-8",
            headers={"Content-Security-Policy": CSP},
        )

    async def page(self, writer: asyncio.StreamWriter, tab: Tab | None) -> None:
        s = self.runner.sessions.get(tab.workspace) if tab is not None else None
        if tab is not None and tab.open and s is not None:
            await live.send(
                writer,
                "302 Found",
                b"",
                headers={"Location": self.runner.takeover(s, tab)},
            )
            return
        what = (
            "This chat's browser tab is closed. It opens again when the agent next browses in this chat."
            if tab is not None
            else "This browser tab isn't known here (the browser service restarted since)."
        )
        await self.note(writer, "Browser", what)


ASKED = {
    "waiting": (
        "running",
        "Open this card to save your login; the agent can use it there, never read it",
    ),
    "saving": ("running", "Saving it in this workspace's logins…"),
    "saved": ("done", "Saved in this workspace's logins; tell the agent in the chat"),
    "declined": ("failed", "You didn't give a login"),
    "expired": ("interrupted", "Nobody answered in time; the agent can ask again"),
}


def asked_picture(req: LoginRequest, state: str, theme: str = THEME) -> bytes:
    """A login request's card as a PNG, in the progress cards' style."""
    look, line = ASKED[state]
    label = f"Browser · {req.workspace} · login · {state}"
    return progress.draw(
        f"Log in to {registrable(req.site)}",  # who the site belongs to, never cut off
        label,
        None if state in ("waiting", "saving") else 1.0,
        line,
        look,
        theme,
    )
