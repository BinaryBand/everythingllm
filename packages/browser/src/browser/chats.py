"""A chat's browser as a client app sees it: given an AnythingLLM developer API key, the
chat's tab and its requests for logins, with their cards and how they stand, as JSON. The
agent puts a card in the chat only when its reply ends, if it does; with this a client can
show the tab's card from the agent's first step and say in words who has the browser, after
the answer too.

    GET /_live/browser/chat/<workspace>/<thread>     Authorization: Bearer <key>

`<thread>` is the thread's slug, as the developer API names it; the runner keeps tabs by
AnythingLLM's thread id (the scope a skill gives it), which only the internal API has, so it
asks that for the workspace's threads (ThreadIds). The answer:

    {"tab": null | {"card", "page", "state", "title", "last"},
     "logins": [{"card", "page", "site", "state"}, ...]}

`card` is the card's picture and `page` where it links, as in the card line the agent gets
(Runner.card); `state` is Runner.state's (working, idle, waiting, user, closed) for the tab
and Runner.asked_state's (waiting, saving, saved, declined, expired) for a login request,
newest first. `title` is the card's name for the page, never its address, and `last` what
was done last, as the card says it.

The key is checked as the relay checks it (KeyCheck: `GET /api/v1/auth`, a good key
remembered by its hash for a minute) and never logged or kept. Anyone with a key can have
the agent hand them a chat's card anyway; without one, a tab's card stays reachable only by
its unguessable id. So this route, and only this one, answers any origin (CORS), which a web
client needs: the tab's picture itself still doesn't.

Config (environment):
  ANYTHINGLLM_URL  AnythingLLM's address (default http://127.0.0.1:3001)
  ANYTHINGLLM_ENV  AnythingLLM's .env, with the password the internal API logs in with
                   (default <ANYTHINGLLM_STORAGE>/.env)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import hostrpc

from browser.origin import registrable

if TYPE_CHECKING:
    from browser.runner import Runner

log = logging.getLogger("browser-runner")

ROUTE = re.compile(
    r"(?:/_live/browser)?/chat/([a-z0-9_][a-z0-9_-]{0,99})/([A-Za-z0-9_-]{1,64})"
)
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization",
    "Access-Control-Allow-Methods": "GET",
    "Access-Control-Max-Age": "600",
}
REFUSED = "No valid api key found."  # AnythingLLM's own words, as the relay answers
REMEMBER = 60.0  # seconds a good key, or a workspace's threads, is remembered
TIMEOUT = 10

# None for a good key, else the status and error to answer with.
Check = Callable[[str], Awaitable[tuple[int, str] | None]]
# A thread's id from its workspace and slug, or None when the workspace has no such thread.
Lookup = Callable[[str, str], Awaitable[str | None]]


def anythingllm_url() -> str:
    return os.environ.get("ANYTHINGLLM_URL", "http://127.0.0.1:3001").rstrip("/")


def get_json(url: str, headers: dict[str, str]) -> tuple[int, Any]:
    """A GET's status and JSON body (None if it isn't JSON), blocking."""
    req = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            status, body = res.status, res.read()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read()
    try:
        return status, json.loads(body)
    except ValueError:
        return status, None


class KeyCheck:
    """Asks AnythingLLM whether a developer API key is good, remembering good ones by their
    hash, so nothing here holds a key."""

    def __init__(self, base_url: str | None = None, now=time.monotonic) -> None:
        self.url = (base_url or anythingllm_url()) + "/api/v1/auth"
        self.now = now
        self.good: dict[bytes, float] = {}

    async def __call__(self, key: str) -> tuple[int, str] | None:
        digest = hashlib.sha256(key.encode()).digest()
        now = self.now()
        if self.good.get(digest, 0) > now:
            return None
        try:
            status, _ = await asyncio.to_thread(
                get_json, self.url, {"Authorization": f"Bearer {key}"}
            )
        except (urllib.error.URLError, OSError) as e:
            # The exception's text can carry the URL; its class says enough for the log.
            log.warning("checking a key failed: %s", type(e).__name__)
            return 502, "AnythingLLM couldn't be reached to check the key."
        if status in (401, 403):
            return 403, REFUSED
        if not 200 <= status < 300:
            log.warning("checking a key answered %d", status)
            return 502, f"AnythingLLM couldn't check the key ({status})."
        self.good = {d: t for d, t in self.good.items() if t > now}
        self.good[digest] = now + REMEMBER
        return None


class ThreadIds:
    """A thread's id from its slug, through AnythingLLM's internal API (its UI's, logged in
    with its password: hostrpc.anythingllm_headers), which lists a workspace's threads with
    both. A workspace's list is kept for a minute, and asked again sooner for a slug it
    lacks, as a chat made since."""

    def __init__(
        self,
        base_url: str | None = None,
        env_file: Path | None = None,
        now=time.monotonic,
    ) -> None:
        self.api = (base_url or anythingllm_url()) + "/api"
        self.env_file = env_file or Path(
            os.environ.get("ANYTHINGLLM_ENV") or hostrpc.storage() / ".env"
        )
        self.now = now
        self.known: dict[
            str, tuple[float, dict[str, str]]
        ] = {}  # workspace -> (when, slug -> id)

    def threads(self, workspace: str) -> dict[str, str]:
        url = f"{self.api}/workspace/{quote(workspace, safe='')}/threads"
        status, body = get_json(
            url, hostrpc.anythingllm_headers(self.api, self.env_file)
        )
        if status == 401:
            status, body = get_json(
                url, hostrpc.anythingllm_headers(self.api, self.env_file, fresh=True)
            )
        if status != 200 or not isinstance(body, dict):
            return {}  # no such workspace, or AnythingLLM's trouble: no chat found
        return {
            str(t["slug"]): str(t["id"])
            for t in body.get("threads") or []
            if isinstance(t, dict) and t.get("slug") and t.get("id") is not None
        }

    async def __call__(self, workspace: str, slug: str) -> str | None:
        when, ids = self.known.get(workspace, (0.0, {}))
        if slug not in ids or self.now() - when > REMEMBER:
            ids = await asyncio.to_thread(self.threads, workspace)
            self.known[workspace] = (self.now(), ids)
        return ids.get(slug)


class Chats:
    """Answers the route for the live cards' server (browser.live)."""

    def __init__(
        self, runner: Runner, check: Check | None = None, lookup: Lookup | None = None
    ) -> None:
        self.runner = runner
        self.check = check or KeyCheck()
        self.lookup = lookup or ThreadIds()

    async def answer(
        self, workspace: str, slug: str, headers: dict[str, str]
    ) -> tuple[str, dict[str, Any]]:
        """The status and JSON body for a GET of the chat's route."""
        scheme, _, key = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not key.strip():
            return "401 Unauthorized", {"error": REFUSED}
        if (refused := await self.check(key.strip())) is not None:
            status, error = refused
            return f"{status} {'Forbidden' if status == 403 else 'Bad Gateway'}", {
                "error": error
            }
        thread = await self.lookup(workspace, slug)
        if thread is None:
            return "404 Not Found", {"error": "No such chat."}
        return "200 OK", self.of(workspace, thread)

    def of(self, workspace: str, thread: str) -> dict[str, Any]:
        """What the runner knows of the chat's browser."""
        r = self.runner
        tab = r.threads.get((workspace, thread))
        r.prune_asked()
        asked = sorted(
            (
                q
                for q in r.asked.values()
                if (q.workspace, q.thread) == (workspace, thread)
            ),
            key=lambda q: q.made,
            reverse=True,
        )
        cards = r.config.pages_url.rstrip("/")
        return {
            "tab": None
            if tab is None or not cards
            else {
                "card": f"{cards}/_live/browser/{tab.id}.jpg",
                "page": f"{cards}/_live/browser/{tab.id}",
                "state": r.state(tab),
                "title": r.subject(tab),
                "last": tab.last,
            },
            "logins": []
            if not cards
            else [
                {
                    "card": f"{cards}/_live/browser/login/{q.id}.png",
                    "page": f"{cards}/_live/browser/login/{q.id}",
                    "site": registrable(q.site),
                    "state": r.asked_state(q),
                }
                for q in asked
            ],
        }
