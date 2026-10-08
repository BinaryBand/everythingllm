"""The apps server: each app's live card for the chat (sandbox.apps), in sandbox-runner.

The machine routes https://<PUBLIC_HOST>:8445/_live/apps/ here (apps.toml's sandbox app), as
it does research's and the browser's cards:

  GET /_live/apps/<workspace>/<name>.png[?theme=light]
      the app's card, live (chatimage.live's server push): a frame now, and a new one
      whenever the app changes, through the app op, its page or a run's edit to its
      data (and any other edit, looked for every POLL seconds), for at most MAX_STREAM. An app that's gone gets
      one frame saying so, so an old chat's card doesn't break.
  GET /_live/apps/<workspace>/<name>
      a redirect to its page, on the workspace pages site (:8447).

and https://<PUBLIC_HOST>:8447/_apps/ here, the page's own origin, for its write-back:

  POST /_apps/<workspace>/<name>/ops   {"token", "op", "args"}, as text/plain
      one of the template's ops, from the app's page (sandbox.apps), applied as the app op
      applies it. The page runs in an opaque origin (the pages CSP's sandbox), so it posts
      text/plain (no preflight) and the answer allows origin "null"; what makes a post the
      page's is its token, the app's current one, which every render replaces: a page
      rendered before gets 409 (reload it), anything else 403. At most MAX_BODY bytes,
      RATE ops per app, and none while a run holds the workspace (409). The answer is the
      app's data and the next token, so the page goes on without a reload.

A card is per app, not per chat: the same address shows the app as it is now in every
chat it was pasted in. Paths are taken with or without their prefix (a route may strip
it). Like the other live servers it answers only loopback and its own address
(hostrpc.local_peer), where the machine's route delivers from.

Config (environment, from the unit):
  APPS_PORT   the port to listen on (default 8455), on 127.0.0.1
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import hostrpc
from chatimage import BAR, PAD, THEMES, WIDTH, font, frame, live
from PIL import Image, ImageDraw

from sandbox import apps
from sandbox.errors import BadToken, Busy, NoSuchApp, StaleToken
from sandbox.names import KEY, SLUG

if TYPE_CHECKING:
    from sandbox.runner import Runner

log = logging.getLogger("sandbox.appsweb")

PORT = 8455
MAX_STREAM = 30 * 60
POLL = 10.0  # a fallback: an op and a run's edit announce themselves
CARD = re.compile(rf"(?:/_live/apps)?/({KEY})/({SLUG})(\.png)?")
OPS = re.compile(rf"(?:/_apps)?/({KEY})/({SLUG})/ops")
MAX_BODY = 4096
RATE = (10, 10.0)  # ops per app in so many seconds
PAGE_CORS = {"Access-Control-Allow-Origin": "null", "Vary": "Origin"}
STATUS = {BadToken: "403 Forbidden", Busy: "409 Conflict", NoSuchApp: "404 Not Found"}


def gone_card(theme: str) -> bytes:
    """The card of an app that's gone."""
    p = THEMES[theme]
    height = 220
    image = Image.new("RGBA", (WIDTH, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    frame(d, height, p.faint, p)
    d.text((BAR + PAD, 54), "App", font=font("regular", 32), fill=p.faint)
    d.text(
        (BAR + PAD, 104), "This app was deleted.", font=font("bold", 54), fill=p.title
    )
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue()


class AppsWeb:
    def __init__(self, runner: Runner):
        self.runner = runner
        self.recent: dict[tuple[str, str], deque[float]] = {}

    async def serve(self, port: int) -> asyncio.Server:
        return await asyncio.start_server(self.handle, "127.0.0.1", port)

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if not (head := await live.accept(reader, writer)):
            return
        method, path, query, headers = head
        try:
            if ops := OPS.fullmatch(path):
                if method == "OPTIONS":
                    return await live.send(
                        writer,
                        "204 No Content",
                        headers={
                            **PAGE_CORS,
                            "Access-Control-Allow-Methods": "POST",
                            "Access-Control-Allow-Headers": "Content-Type",
                        },
                    )
                if method != "POST":
                    return await live.send(
                        writer, "405 Method Not Allowed", b"POST only.\n"
                    )
                return await self.write_back(reader, writer, ops[1], ops[2], headers)
            route = CARD.fullmatch(path)
            if method != "GET":
                return await live.send(writer, "405 Method Not Allowed", b"GET only.\n")
            if not route:
                return await live.send(writer, "404 Not Found", b"No such app.\n")
            workspace, name, image = route[1], route[2], bool(route[3])
            if not image:
                page = self.runner.public_url(workspace, f"apps/{name}/")
                return await live.send(writer, "302 Found", headers={"Location": page})
            theme = live.theme(query)
            s = self.runner.app_scope(workspace)
            if s is None:
                card = await asyncio.to_thread(gone_card, theme)
                return await live.send(writer, "200 OK", card, "image/png")
            await live.push(writer, self.frames(s, name, theme))
        except Exception:  # one viewer's trouble mustn't reach the runner
            log.exception("apps server: %s %s failed", method, path)
            if not writer.is_closing():
                await live.send(
                    writer, "500 Internal Server Error", b"Something went wrong.\n"
                )

    def allowed(self, key: tuple[str, str]) -> bool:
        """Whether the app may take another op now (RATE)."""
        most, seconds = RATE
        now = time.monotonic()
        for quiet in [k for k, q in self.recent.items() if q[-1] < now - seconds]:
            del self.recent[quiet]  # so the apps posted to long ago aren't kept
        recent = self.recent.setdefault(key, deque())
        while recent and recent[0] < now - seconds:
            recent.popleft()
        if len(recent) >= most:
            return False
        recent.append(now)
        return True

    async def write_back(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        workspace: str,
        name: str,
        headers: dict[str, str],
    ) -> None:
        """An op from the app's page (the module's docstring has the rules)."""

        async def answer(status: str, body: dict) -> None:
            await live.send(
                writer, status, json.dumps(body).encode(), "application/json", PAGE_CORS
            )

        try:
            length = int(headers.get("content-length", ""))
        except ValueError:
            return await answer("411 Length Required", {"error": "send a body"})
        if not 0 < length <= MAX_BODY:
            return await answer(
                "413 Content Too Large", {"error": "the request is too big"}
            )
        try:
            body = await asyncio.wait_for(reader.readexactly(length), 10)
            msg = json.loads(body)
            token, op = msg["token"], msg["op"]
            args = msg.get("args") or {}
            if not (
                isinstance(token, str)
                and isinstance(op, str)
                and isinstance(args, dict)
            ):
                raise TypeError
        except (
            TimeoutError,
            asyncio.IncompleteReadError,
            ValueError,
            KeyError,
            TypeError,
        ):
            return await answer("400 Bad Request", {"error": "not an op"})
        s = self.runner.app_scope(workspace)
        if s is None:
            return await answer("404 Not Found", {"error": "no such app"})
        if not self.allowed((workspace, name)):
            return await answer(
                "429 Too Many Requests", {"error": "too fast; wait a moment"}
            )
        try:
            async with self.runner.exclusive(workspace):
                data, _, token = await asyncio.to_thread(
                    self.runner.change_app, s, name, op, args, token
                )
        except StaleToken as e:
            return await answer("409 Conflict", {"error": str(e), "reload": True})
        except (BadToken, Busy, NoSuchApp) as e:
            return await answer(STATUS[type(e)], {"error": str(e)})
        except hostrpc.RunnerError as e:
            return await answer("400 Bad Request", {"error": str(e)})
        await self.runner.app_changed()
        log.info("%s/%s: %s from its page", workspace, name, op)
        return await answer("200 OK", {"data": data, "token": token})

    def stamp(self, s, name: str) -> tuple:
        """What changes when the app does: its data file's identity and time."""
        try:
            st = os.stat(
                s.roots["/project"] / "apps" / name / "data.json", follow_symlinks=False
            )
        except OSError:
            return ()
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    async def frames(self, s, name: str, theme: str) -> AsyncIterator[bytes]:
        """A frame now, then one whenever the app changes, until MAX_STREAM or it's gone."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + MAX_STREAM
        shown = None
        while True:
            stamp = await asyncio.to_thread(self.stamp, s, name)
            if stamp != shown:
                shown = stamp
                try:
                    data = await asyncio.to_thread(self.runner.read_app, s, name)
                except hostrpc.RunnerError:
                    yield await asyncio.to_thread(gone_card, theme)
                    return
                yield await asyncio.to_thread(apps.of(data).card, data, theme)
            if loop.time() > deadline:
                return
            changed = self.runner.apps_changed
            async with changed:
                try:
                    await asyncio.wait_for(changed.wait(), POLL)
                except TimeoutError:
                    pass
