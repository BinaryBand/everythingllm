"""The browser's live cards: each chat thread's tab as a picture in the chat that keeps
showing what the page looks like as the agent (or the user) works in it.

The skills hand the agent a card line for the tab (Runner.card):

    [![Browser: <title>](https://<host>:8445/_live/browser/<id>.jpg)](https://<host>:8445/_live/browser/<id>)

`tailscale serve` maps https://<host>:8445/_live/browser to this server's port (apps.toml)
and strips the prefix, so paths are taken with or without it.

- `<id>.jpg` is the card: a screenshot of the tab under a strip saying whose hands it's in
  and what was done last, pushed again (chatimage.live, as JPEG) whenever it changes,
  checked every GAP seconds, until MAX_STREAM passes. A card being watched keeps the
  browser from being stopped as idle. A closed tab shows its last screenshot, dimmed, and
  the stream waits for it to open again. A tab the runner doesn't know (from before a
  restart) gets one frame saying so.
- `<id>` is where the card links: the take-over view of the tab's browser while it runs
  (a redirect to browser.takeover, whose address changes with each container), else a
  page saying it's closed.

A tab's id is `bw-` and 16 hex digits, not guessable: the card and its link are the only
way to it. A connection from anywhere but loopback or the server's own address is refused
(hostrpc.local_peer).

Config (environment):
  LIVE_HOST  the address to listen on (default 127.0.0.1)
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import io
import os
import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import hostrpc
from chatimage import (
    BACKGROUND,
    FAINT,
    MUTED,
    TITLE,
    accent_for,
    clean,
    fit,
    font,
    live,
    progress,
)
from PIL import Image, ImageDraw, ImageEnhance

if TYPE_CHECKING:
    from browser.runner import Runner, Tab

HOST = "127.0.0.1"
CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
WIDTH = 1280  # the screen's width; the chat shows it at up to 800
STRIP = 132  # the strip above the screenshot
USER = (242, 181, 107)  # the strip's colour while the user has the browser
STATES = {
    "agent": "the agent is browsing",
    "user": "you have it",
    "closed": "closed",
}


def picture(
    shot: bytes, workspace: str, state: str, title: str, url: str, last: str
) -> bytes:
    """A frame as a JPEG: the strip (who has the browser, the page's title, its address and
    what was done last) above the screenshot, dimmed once the tab is closed."""
    accent = {"user": USER, "closed": FAINT}.get(state, accent_for("Browser"))
    try:
        screen = Image.open(io.BytesIO(shot)).convert("RGB") if shot else None
    except OSError:
        screen = None
    if screen is not None and screen.width != WIDTH:
        screen = screen.resize((WIDTH, round(screen.height * WIDTH / screen.width)))
    height = screen.height if screen is not None else 360
    image = Image.new("RGB", (WIDTH, STRIP + height), BACKGROUND)
    d = ImageDraw.Draw(image)
    d.rectangle((0, 0, 10, STRIP), fill=accent)
    x, width = 36, WIDTH - 72
    small, big = font("regular", 26), font("bold", 36)
    label = clean(f"Browser · {workspace} · {STATES.get(state, state)}")
    d.text((x, 16), fit(d, label, small, width), font=small, fill=accent)
    d.text(
        (x, 50),
        fit(d, clean(title) or clean(url) or "A new tab", big, width),
        font=big,
        fill=TITLE,
    )
    line = " · ".join(t for t in (clean(url), clean(last)) if t)
    d.text((x, 96), fit(d, line, small, width), font=small, fill=MUTED)
    if screen is not None:
        if state == "closed":
            screen = ImageEnhance.Brightness(screen).enhance(0.4)
        image.paste(screen, (0, STRIP))
    else:
        note = "Nothing to show yet" if state != "closed" else "The browser is closed"
        d.text((x, STRIP + 150), note, font=big, fill=FAINT)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=72, optimize=True)
    return out.getvalue()


class Live:
    PATH = "/_live/browser/"
    GAP = 1.0  # seconds between looks at an open tab
    TICK = 30.0  # seconds between frames at most, the same one again if nothing changed
    MAX_STREAM = 30 * 60  # seconds one connection is pushed frames; a reload asks again
    ROUTE = re.compile(r"(?:/_live/browser)?/(bw-[0-9a-f]{16})(\.jpg)?")

    def __init__(self, runner: Runner):
        self.runner = runner

    async def serve(self, port: int) -> asyncio.Server:
        return await asyncio.start_server(
            self.handle, os.environ.get("LIVE_HOST") or HOST, port
        )

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            method, path = await asyncio.wait_for(live.read_request(reader), 10)
        except (live.BadRequest, TimeoutError):
            return await live.send(writer, "400 Bad Request", b"Bad request.\n")
        if not hostrpc.local_peer(
            writer.get_extra_info("peername"), writer.get_extra_info("sockname")
        ):
            return await live.send(writer, "403 Forbidden", b"Not from here.\n")
        if method != "GET":
            return await live.send(writer, "405 Method Not Allowed", b"GET only.\n")
        route = self.ROUTE.fullmatch(path)
        if not route:
            return await live.send(writer, "404 Not Found", b"No such tab.\n")
        tab = self.runner.tabs.get(route[1])
        try:
            if route[2] and tab is not None:
                await self.stream(reader, writer, tab)
            elif route[2]:
                frame = await asyncio.to_thread(
                    progress.draw,
                    "This browser tab isn't open here",
                    "Browser",
                    None,
                    "It was open before the browser service restarted.",
                    "interrupted",
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

    async def stream(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, tab: Tab
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
            await live.push(writer, self.frames(tab, gone), "image/jpeg")
        finally:
            watcher.cancel()

    async def frames(
        self, tab: Tab, gone: asyncio.Event | None = None
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
                    yield await asyncio.to_thread(
                        picture,
                        shot,
                        tab.workspace,
                        state,
                        tab.title,
                        tab.url,
                        tab.last,
                    )
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
        body = (
            "<!doctype html><html lang='en'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Browser</title>"
            "<body style='font:16px/1.5 system-ui,sans-serif;max-width:40em;margin:3em auto;padding:0 1em'>"
            f"<h1>Browser</h1><p>{html.escape(what)}</p></body></html>"
        ).encode()
        await live.send(
            writer,
            "200 OK",
            body,
            "text/html; charset=utf-8",
            headers={"Content-Security-Policy": CSP},
        )
