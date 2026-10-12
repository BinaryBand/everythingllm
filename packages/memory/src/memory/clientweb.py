"""The long-term memory's client route: what a client holding the user's AnythingLLM
developer API key reads and changes in it, at https://<PUBLIC_HOST>:8445/_live/memory/
(`serve` in hostctl's apps.toml; a path without that prefix counts too, should the
machine's route strip it). `<workspace>` is AnythingLLM's slug, as a skill's scope names it.

  GET  /_live/memory/<workspace>
      {workspace, count, facts: [{id, scope, text, created}], files: [{path, parts,
      ingested}], waiting: [{path, parts}], failed: [{path, error}]}: the newest
      CLIENT_FACTS facts the workspace's chats see (global and its own), newest first, and
      `count`, how many there are; `id` is m<number> and `scope` global or workspace.
      `files` are its ingested files by path, `waiting` those the ingest queue still holds
      (`parts` chunks still to embed) and `failed` those it gave up on.
  GET  /_live/memory/<workspace>/recall?q=<query>[&k=<1 to 20>]
      {hits}, closest first, each a fact {score, id, scope, text, created} or a file's
      chunk {score, path, part, text, ingested}; 503 while the model can't answer yet.
  POST /_live/memory/<workspace>/remember        {"text", "scope": workspace | global}
      {fact, new}: the fact kept, or the one that already says it (`new` false); 400 for
      a text hostenv.fact_text refuses, 409 for a scope that holds runner.MAX_FACTS.
  POST /_live/memory/<workspace>/forget?id=m<number>
  POST /_live/memory/<workspace>/forget?path=<path>
      {forgotten: id}, a fact the workspace's chats see, or {dropped: files}, the text
      ingested from a path in `files` (a file or folder; the files themselves stay);
      404 for none.

All with `Authorization: Bearer <key>` (hostenv.keys: checked with AnythingLLM's
/api/v1/auth, never kept), answering any origin (CORS), as sandbox.clientweb's routes do:
a key already reads every workspace's chats.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import parse_qs

import hostenv.keys
from chatimage import live
from hostenv import JOBS, WORKSPACE_RE, fact_text, whole_number
from hostrpc import RunnerError

from memory.runner import HOST, MemoryService, fact_id
from memory.store import GLOBAL, Fact, Full

ROUTE = re.compile(r"(?:/_live/memory)?/([^/]+)(?:/(recall|remember|forget))?")
READS = (None, "recall")  # GET; the others POST
CLIENT_FACTS = 500  # newest facts the listing gives
MAX_BODY = 8 * 1024  # remember's JSON
BODY_SECONDS = 10  # for it to come in
CORS = {
    **hostenv.keys.CLIENT_CORS,
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
}


class Refused(Exception):
    """A request answered with `status` and the error's text."""

    def __init__(self, status: str, error: str):
        super().__init__(error)
        self.status = status


async def reply(writer: asyncio.StreamWriter, status: str, body: dict) -> None:
    await live.send(
        writer,
        status,
        json.dumps(body, ensure_ascii=False).encode(),
        "application/json",
        CORS,
    )


def workspace_of(key: str) -> str | None:
    """The workspace a route names, if it's one a client may read: never a scheduled
    job's or the CLI's, which aren't workspaces."""
    return key if WORKSPACE_RE.fullmatch(key) and key not in (JOBS, HOST) else None


def fact_json(fact: Fact) -> dict[str, Any]:
    return {
        "id": f"m{fact.id}",
        "scope": GLOBAL if fact.scope == GLOBAL else "workspace",
        "text": fact.text,
        "created": fact.created,
    }


def last(query: dict[str, list[str]], name: str) -> str:
    return (query.get(name) or [""])[-1]


async def read_json(
    reader: asyncio.StreamReader, headers: dict[str, str]
) -> dict[str, Any]:
    """The request's body, a JSON object of at most MAX_BODY bytes."""
    length = whole_number(headers.get("content-length", ""))
    if length is None:
        raise Refused("411 Length Required", "send the body with its Content-Length")
    if length > MAX_BODY:
        raise Refused("413 Content Too Large", "the body is too big")
    try:
        raw = await asyncio.wait_for(reader.readexactly(length), BODY_SECONDS)
    except (asyncio.IncompleteReadError, TimeoutError):
        raise Refused("400 Bad Request", "the body didn't come in") from None
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise Refused("400 Bad Request", "send a JSON object")
    return body


class ClientWeb:
    def __init__(self, service: MemoryService, check: hostenv.keys.Check):
        self.service = service
        self.check = check

    async def serve(self, port: int, host: str = "127.0.0.1") -> asyncio.Server:
        return await asyncio.start_server(self.handle, host, port)

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Answer one request: preflight, the method, then the key, then the route."""
        if not (head := await live.accept(reader, writer)):
            return
        method, path, query, headers = head
        route = ROUTE.fullmatch(path)
        workspace = route and workspace_of(route[1])
        if not route or not workspace:
            return await reply(writer, "404 Not Found", {"error": "no such route"})
        if method == "OPTIONS":
            return await live.send(writer, "204 No Content", headers=CORS)
        action = route[2]
        if method != ("GET" if action in READS else "POST"):
            return await reply(
                writer,
                "405 Method Not Allowed",
                {"error": "GET to read, POST to change"},
            )
        if (refused := await hostenv.keys.refusal(self.check, headers)) is not None:
            status, error = refused
            return await reply(writer, status, {"error": error})
        asked = parse_qs(query)
        try:
            if action is None:
                body = await asyncio.to_thread(self.listing, workspace)
            elif action == "recall":
                body = await asyncio.to_thread(self.recall, workspace, asked)
            elif action == "remember":
                sent = await read_json(reader, headers)
                body = await asyncio.to_thread(self.remember, workspace, sent)
            else:
                body = await asyncio.to_thread(self.forget, workspace, asked)
        except Refused as e:
            return await reply(writer, e.status, {"error": str(e)})
        except RunnerError as e:  # the model isn't ready (Model.wait)
            return await reply(writer, "503 Service Unavailable", {"error": str(e)})
        return await reply(writer, "200 OK", body)

    def listing(self, workspace: str) -> dict[str, Any]:
        store, ingester = self.service.store, self.service.ingester
        facts, count = store.facts([GLOBAL, workspace], CLIENT_FACTS)
        return {
            "workspace": workspace,
            "count": count,
            "facts": [fact_json(f) for f in facts],
            "files": [
                {"path": path, "parts": parts, "ingested": when}
                for path, parts, when in store.files(workspace)
            ],
            "waiting": [
                {"path": path, "parts": parts}
                for path, parts in sorted(ingester.waiting(workspace).items())
            ],
            "failed": [
                {"path": path, "error": why}
                for path, why in sorted(ingester.failures(workspace).items())
            ],
        }

    def recall(self, workspace: str, asked: dict[str, list[str]]) -> dict[str, Any]:
        try:
            found = self.service.op_recall(
                {"workspace": workspace}, last(asked, "q"), last(asked, "k") or None
            )
        except RunnerError as e:
            if self.service.model.ready:
                raise Refused("400 Bad Request", str(e)) from None
            raise
        hits = []
        for hit in found["hits"]:
            if "id" in hit:
                hit = {
                    **hit,
                    "id": f"m{hit['id']}",
                    "scope": GLOBAL if hit["scope"] == GLOBAL else "workspace",
                }
            hits.append(hit)
        return {"hits": hits}

    def remember(self, workspace: str, sent: dict[str, Any]) -> dict[str, Any]:
        kind = str(sent.get("scope") or "workspace").strip().lower()
        if kind not in ("workspace", GLOBAL):
            raise Refused(
                "400 Bad Request", "scope is workspace (the default) or global"
            )
        try:
            text = fact_text(sent.get("text"))
        except ValueError as e:
            raise Refused("400 Bad Request", str(e)) from None
        try:
            fact, new = self.service.keep(
                workspace if kind == "workspace" else GLOBAL, text
            )
        except Full:
            raise Refused(
                "409 Conflict", "that scope holds as many facts as it keeps"
            ) from None
        return {"fact": fact_json(fact), "new": new}

    def forget(self, workspace: str, asked: dict[str, list[str]]) -> dict[str, Any]:
        store = self.service.store
        if path := last(asked, "path"):
            if not (dropped := store.drop_file(workspace, path)):
                raise Refused("404 Not Found", f"nothing ingested from '{path}'")
            return {"dropped": dropped}
        number = fact_id(last(asked, "id"))
        if number is None:
            raise Refused("400 Bad Request", "give id=m<number> or path=<path>")
        if (fact := store.delete(number, [GLOBAL, workspace])) is None:
            raise Refused("404 Not Found", f"no fact m{number} here")
        return {"forgotten": f"m{fact.id}"}
