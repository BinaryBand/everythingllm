"""The apps server: each app's live card for the chat (sandbox.apps), in sandbox-runner.

The machine routes https://<PUBLIC_HOST>:8445/_live/apps/ here (apps.toml's sandbox app), as
it does research's and the browser's cards:

  GET /_live/apps/<workspace>/<name>.png[?theme=light]
      the app's card, live (chatimage.live's server push): a frame now, and a new one
      whenever the app changes, through the app op, its page or a run's edit to its
      data (and any other edit, looked for every POLL seconds), for at most MAX_STREAM. An app that's gone gets
      one frame saying so, so an old chat's card doesn't break; one whose data can't be
      used says that, and shows the app again once it's fixed.
  GET /_live/apps/<workspace>/<name>
      a redirect to its page, on the workspace pages site (:8447).

and https://<PUBLIC_HOST>:8447/_apps/ here, the page's own origin, for its write-back
(sandbox.apps; its client is apps/client.js, the page's app.js):

  POST /_apps/<workspace>/<name>/ops   {"token", "patch", "version"?}, as text/plain
      a JSON patch on the app's title, card and data, applied as the app op applies it,
      and only to `version` when it's given: one behind gets 409 {"conflict": true, and
      the app as it is now} to start again from. The answer is {"version"}.
  POST /_apps/<workspace>/<name>/sync  {"token", "since"}
      the app ({title, version, card, data}) once its version isn't `since`, waiting up to
      SYNC_WAIT for that, else {"version"}: how an open page sees another tab's, the
      agent's or a run's change. At most SYNC_WAITERS waiting per app.

  The page runs in an opaque origin (the pages CSP's sandbox), so it posts text/plain (no
  preflight) and the answer allows origin "null"; what makes a post the page's is its
  token, the app's current one, which lasts as long as its page.html: a page from before
  that changed gets 409 {"reload": true}, anything else 403. A write is at most MAX_BODY
  bytes, RATE a while per app, and none while a run holds the workspace (409).

and a client's answer to a request to turn a workspace's access on (sandbox.accessreq), for
a chat that can't ask the user in AnythingLLM's own window, at https://<PUBLIC_HOST>:8445
/_live/sandbox/ (the prefix is required here):

  GET  /_live/sandbox/access/<workspace>
      {workspace, access: {web, models, daily_tokens, heavy}, requests: [{id, state, text,
      would, made, expires}]}, newest first; state is waiting, approved, declined or
      expired, `text` what the user is asked to approve.
  POST /_live/sandbox/access/<workspace>/<id>/approve   (or /decline)
      applies (or drops) the change the request asked for, once: the request as it now
      stands, or 404 for none, 409 for one answered or expired.

  All with `Authorization: Bearer <key>`, an AnythingLLM developer API key (hostenv.keys:
  checked with /api/v1/auth, never kept), and answering any origin (CORS), since only a key
  does. This is the only way a request is answered: no op a skill calls approves one.

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
from dataclasses import asdict
from http import HTTPStatus
from typing import TYPE_CHECKING

import hostenv.keys
import hostrpc
from chatimage import BAR, PAD, THEMES, WIDTH, font, frame, live
from PIL import Image, ImageDraw

from sandbox import apps
from sandbox.access import read_access
from sandbox.accessreq import NoRequest, NotWaiting
from sandbox.errors import BadToken, Busy, Conflict, NoSuchApp, StaleToken
from sandbox.names import KEY, SLUG

if TYPE_CHECKING:
    from sandbox.runner import Runner

log = logging.getLogger("sandbox.appsweb")

PORT = 8455
MAX_STREAM = 30 * 60
POLL = 10.0  # a fallback: an op and a run's edit announce themselves
CARD = re.compile(rf"(?:/_live/apps)?/({KEY})/({SLUG})(\.png)?")
OPS = re.compile(rf"(?:/_apps)?/({KEY})/({SLUG})/(ops|sync)")
ACCESS = re.compile(
    rf"/_live/sandbox/access/({KEY})(?:/(ar-[0-9a-f]{{32}})/(approve|decline))?"
)
CLIENT_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization",
    "Access-Control-Allow-Methods": "GET, POST",
    "Access-Control-Max-Age": "600",
}
MAX_BODY = 256 << 10
RATE = (20, 10.0)  # writes per app in so many seconds
SYNC_WAIT = 25.0
SYNC_WAITERS = 8  # per app
PAGE_CORS = {"Access-Control-Allow-Origin": "null", "Vary": "Origin"}
STATUS = {BadToken: "403 Forbidden", Busy: "409 Conflict", NoSuchApp: "404 Not Found"}


def refusal(e: hostrpc.RunnerError) -> tuple[str, dict]:
    """The answer to a write-back or a sync the runner refused."""
    if isinstance(e, StaleToken):
        return "409 Conflict", {"error": str(e), "reload": True}
    if isinstance(e, Conflict):
        return "409 Conflict", {"error": str(e), "conflict": True, **e.app}
    return STATUS.get(type(e), "400 Bad Request"), {"error": str(e)}


GONE = "This app was deleted."
BROKEN = "This app's data can't be shown."


def gone_card(theme: str, says: str = GONE) -> bytes:
    """The card of an app that's gone, or (`says` BROKEN) whose data can't be used."""
    p = THEMES[theme]
    height = 220
    image = Image.new("RGBA", (WIDTH, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    frame(d, height, p.faint, p)
    d.text((BAR + PAD, 54), "App", font=font("regular", 32), fill=p.faint)
    d.text((BAR + PAD, 104), says, font=font("bold", 54), fill=p.title)
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue()


class AppsWeb:
    def __init__(self, runner: Runner, check: hostenv.keys.Check | None = None):
        self.runner = runner
        self.check = check or hostenv.keys.KeyCheck()
        self.recent: dict[tuple[str, str], deque[float]] = {}
        self.waiting: dict[tuple[str, str], int] = {}
        # Each synced app's data file stamp and the version it held, so a sync of an app
        # that hasn't changed is answered from a stat, not a parse.
        self.versions: dict[tuple[str, str], tuple[tuple, int]] = {}

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
                return await self.write_back(
                    reader, writer, ops[1], ops[2], ops[3], headers
                )
            if access := ACCESS.fullmatch(path):
                return await self.access(writer, method, access.groups(), headers)
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

    async def access(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        parts: tuple[str, str | None, str | None],
        headers: dict[str, str],
    ) -> None:
        """A client's view of a workspace's access requests, or its answer to one."""
        workspace, request_id, action = parts

        async def reply(status: str, body: dict) -> None:
            await live.send(
                writer,
                status,
                json.dumps(body, ensure_ascii=False).encode(),
                "application/json",
                CLIENT_CORS,
            )

        if method == "OPTIONS":
            return await live.send(writer, "204 No Content", headers=CLIENT_CORS)
        if method != ("GET" if request_id is None else "POST"):
            return await reply(
                "405 Method Not Allowed", {"error": "GET the list, POST an answer"}
            )
        scheme, _, key = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not key.strip():
            return await reply("401 Unauthorized", {"error": hostenv.keys.REFUSED})
        if (refused := await self.check(key.strip())) is not None:
            status, error = refused
            return await reply(
                f"{status} {HTTPStatus(status).phrase}", {"error": error}
            )
        requests = self.runner.access_requests
        if request_id is None:
            current = await asyncio.to_thread(
                read_access, self.runner.config.access_file, workspace
            )
            return await reply(
                "200 OK",
                {
                    "workspace": workspace,
                    "access": asdict(current),
                    "requests": [requests.view(r) for r in requests.listing(workspace)],
                },
            )
        try:
            done = await self.runner.answer_access(
                workspace, request_id, action == "approve"
            )
        except NoRequest as e:
            return await reply("404 Not Found", {"error": str(e)})
        except NotWaiting as e:
            return await reply("409 Conflict", {"error": str(e)})
        return await reply("200 OK", done)

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
        kind: str,
        headers: dict[str, str],
    ) -> None:
        """A patch or a sync from the app's page (the module's docstring has the rules)."""

        async def answer(status: str, body: dict) -> None:
            await live.send(
                writer,
                status,
                json.dumps(body, ensure_ascii=False).encode(),
                "application/json",
                PAGE_CORS,
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
            token = msg["token"]
            if not isinstance(token, str):
                raise TypeError
        except (
            TimeoutError,
            asyncio.IncompleteReadError,
            ValueError,
            KeyError,
            TypeError,
        ):
            return await answer("400 Bad Request", {"error": "not a request"})
        s = self.runner.app_scope(workspace)
        if s is None:
            return await answer("404 Not Found", {"error": "no such app"})
        if kind == "sync":
            return await self.sync(answer, s, name, token, msg.get("since"))
        patch, version = msg.get("patch"), msg.get("version")
        if not isinstance(patch, list) or not (
            version is None or isinstance(version, int)
        ):
            return await answer("400 Bad Request", {"error": "not a patch"})
        if not self.allowed((workspace, name)):
            return await answer(
                "429 Too Many Requests", {"error": "too fast; wait a moment"}
            )
        try:
            async with self.runner.exclusive(workspace):
                data, did = await asyncio.to_thread(
                    self.runner.change_app, s, name, patch, version, token
                )
        except hostrpc.RunnerError as e:
            return await answer(*refusal(e))
        await self.runner.app_changed()
        log.info("%s/%s: %s from its page", workspace, name, did)
        return await answer("200 OK", {"version": data["version"]})

    async def sync(self, answer, s, name: str, token: str, since) -> None:
        """The app once its version isn't `since`, or {"version"} after SYNC_WAIT."""
        try:
            self.runner.app_tokens.check(s.workspace, name, token)
        except hostrpc.RunnerError as e:
            return await answer(*refusal(e))
        key = (s.workspace, name)
        if self.waiting.get(key, 0) >= SYNC_WAITERS:
            return await answer(
                "429 Too Many Requests", {"error": "too many open pages of this app"}
            )
        self.waiting[key] = self.waiting.get(key, 0) + 1
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SYNC_WAIT
        version = since
        try:
            while True:
                # Only a data file that changed is read again: any app's change wakes it.
                stamp = await asyncio.to_thread(self.stamp, s, name)
                known = self.versions.get(key)
                if known and known[0] == stamp:
                    version = known[1]
                if not (known and known[0] == stamp and version == since):
                    try:
                        data = await asyncio.to_thread(self.runner.read_app, s, name)
                    except hostrpc.RunnerError as e:
                        self.versions.pop(key, None)
                        return await answer(*refusal(e))
                    version = data["version"]
                    self.versions[key] = (stamp, version)
                    if version != since:
                        return await answer("200 OK", data)
                left = deadline - loop.time()
                if left <= 0:
                    return await answer("200 OK", {"version": version})
                await self.changed_within(min(left, POLL))
        finally:
            self.waiting[key] -= 1
            if not self.waiting[key]:
                del self.waiting[key]

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
                except NoSuchApp:
                    yield await asyncio.to_thread(gone_card, theme)
                    return
                except hostrpc.RunnerError:  # data a run broke: drawn again once fixed
                    yield await asyncio.to_thread(gone_card, theme, BROKEN)
                else:
                    yield await asyncio.to_thread(apps.card, data, theme)
            if loop.time() > deadline:
                return
            await self.changed_within(POLL)

    async def changed_within(self, seconds: float) -> None:
        """Back when an app changes (the runner's apps_changed), or after `seconds`."""
        changed = self.runner.apps_changed
        async with changed:
            try:
                await asyncio.wait_for(changed.wait(), seconds)
            except TimeoutError:
                pass
