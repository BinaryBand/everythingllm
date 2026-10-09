"""The client routes of the apps server (sandbox.appsweb): what a client holding the
user's developer API key reads and changes in a workspace, at
https://<PUBLIC_HOST>:8445/_live/sandbox/ (the machine's route strips that prefix, so a
path without it counts too, but only from a request with a key or a preflight: an app
named `access` or `files` keeps its page's redirect for everyone else).

A chat that can't ask the user in AnythingLLM's own window has its request to turn a
workspace's access on answered here (sandbox.accessreq):

  GET  /_live/sandbox/access/<workspace>
      {workspace, access: {web, models, daily_tokens, heavy}, requests: [{id, state, text,
      would, made, expires}]}, newest first; state is waiting, approved, declined or
      expired, `text` what the user is asked to approve.
  POST /_live/sandbox/access/<workspace>/<id>/approve   (or /decline)
      applies (or drops) the change the request asked for, once: the request as it now
      stands, or 404 for none, 409 for one answered or expired.

and the workspace's created files (sandbox.created) are listed and managed:

  GET  /_live/sandbox/files/<workspace>
      {workspace, used, warn, limit, truncated, files: [{id, name, dir, size, modified,
      area, thread?, expires?, url?}]}, newest first: `area` is project, work (a chat's
      working file, which goes at `expires`), public (on the web, at `url`) or shared.
  GET  /_live/sandbox/files/<workspace>/download?path=<id>
      the file, as an attachment (at most created.MAX_READ, else 413).
  POST /_live/sandbox/files/<workspace>/keep?path=<id>      a working file moved to /project
  POST /_live/sandbox/files/<workspace>/delete?path=<id>    one file deleted
      both 409 while a run holds the workspace; a file id that names nothing is 404.

All with `Authorization: Bearer <key>`, an AnythingLLM developer API key (hostenv.keys:
checked with /api/v1/auth, never kept), and answering any origin (CORS), since only a key
does. This is the only way a request is answered: no op a skill calls approves one.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import parse_qs, quote

import hostenv.keys
from chatimage import live

from sandbox import created
from sandbox.access import read_access
from sandbox.accessreq import NoRequest, NotWaiting
from sandbox.errors import SandboxError
from sandbox.names import KEY

if TYPE_CHECKING:
    from sandbox.runner import Runner

ACCESS = re.compile(
    rf"(?:/_live/sandbox)?/access/({KEY})(?:/(ar-[0-9a-f]{{32}})/(approve|decline))?"
)
PREFIX = "/_live/sandbox/"
FILES = re.compile(rf"(?:/_live/sandbox)?/files/({KEY})(?:/(download|keep|delete))?")
CORS = hostenv.keys.CLIENT_CORS


class Route(NamedTuple):
    kind: str  # access or files
    workspace: str
    request_id: str | None  # access: the request answered, if one
    action: str | None
    reads: bool  # GET to read, else POST to change


def route(path: str) -> Route | None:
    """The client route a path names (with or without the machine's prefix), if any."""
    if m := ACCESS.fullmatch(path):
        return Route("access", m[1], m[2], m[3], m[2] is None)
    if m := FILES.fullmatch(path):
        return Route("files", m[1], None, m[2], m[2] in (None, "download"))
    return None


async def reply(writer: asyncio.StreamWriter, status: str, body: dict) -> None:
    await live.send(
        writer,
        status,
        json.dumps(body, ensure_ascii=False).encode(),
        "application/json",
        CORS,
    )


class ClientWeb:
    def __init__(self, runner: Runner, check: hostenv.keys.Check):
        self.runner = runner
        self.check = check

    @staticmethod
    def owns(path: str, method: str, headers: dict[str, str]) -> bool:
        """Whether the request is a client route's (see the module's docstring)."""
        if route(path) is None:
            return False
        return (
            path.startswith(PREFIX) or method == "OPTIONS" or "authorization" in headers
        )

    async def handle(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        query: str,
        headers: dict[str, str],
    ) -> None:
        """Answer a request `owns` took: preflight, the method, then the key, then the
        route."""
        r = route(path)
        assert r
        if method == "OPTIONS":
            return await live.send(writer, "204 No Content", headers=CORS)
        if method != ("GET" if r.reads else "POST"):
            return await reply(
                writer,
                "405 Method Not Allowed",
                {"error": "GET to read, POST to change"},
            )
        if (refused := await hostenv.keys.refusal(self.check, headers)) is not None:
            status, error = refused
            return await reply(writer, status, {"error": error})
        if r.kind == "access":
            return await self.access(writer, r.workspace, r.request_id, r.action)
        return await self.files(writer, query, r.workspace, r.action)

    async def access(
        self,
        writer: asyncio.StreamWriter,
        workspace: str,
        request_id: str | None,
        action: str | None,
    ) -> None:
        """A workspace's access requests, or the answer to one."""
        requests = self.runner.access_requests
        if request_id is None:
            current = await asyncio.to_thread(
                read_access, self.runner.config.access_file, workspace
            )
            return await reply(
                writer,
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
            return await reply(writer, "404 Not Found", {"error": str(e)})
        except NotWaiting as e:
            return await reply(writer, "409 Conflict", {"error": str(e)})
        return await reply(writer, "200 OK", done)

    async def files(
        self,
        writer: asyncio.StreamWriter,
        query: str,
        workspace: str,
        action: str | None,
    ) -> None:
        """A workspace's created files, or a change to one."""
        s = self.runner.app_scope(workspace)
        if s is None:
            return await reply(
                writer, "404 Not Found", {"error": "no sandbox for this workspace"}
            )
        file_id = (parse_qs(query).get("path") or [""])[-1]
        try:
            if action is None:
                body = await asyncio.to_thread(created.listing, self.runner.config, s)
                return await reply(writer, "200 OK", body)
            if action == "download":
                name, data = await asyncio.to_thread(created.read, s, file_id)
                disposition = "attachment; filename*=UTF-8''" + quote(name)
                return await live.send(
                    writer,
                    "200 OK",
                    data,
                    "application/octet-stream",
                    {**CORS, "Content-Disposition": disposition},
                )
            change = created.keep if action == "keep" else created.remove
            async with self.runner.exclusive(workspace):
                done = await asyncio.to_thread(change, s, file_id)
            return await reply(writer, "200 OK", done)
        except created.TooBig as e:
            return await reply(writer, "413 Content Too Large", {"error": str(e)})
        except created.NoSuchFile as e:
            return await reply(writer, "404 Not Found", {"error": str(e)})
        except SandboxError as e:  # Busy too: a run holds the workspace
            return await reply(writer, "409 Conflict", {"error": str(e)})
