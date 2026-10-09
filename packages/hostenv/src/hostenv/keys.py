"""Checking a client's AnythingLLM developer API key (KeyCheck), for the host services that
answer a client over HTTP: the browser's chat route and the sandbox's approval route. A good
key is remembered by its hash for REMEMBER seconds, so nothing here keeps a key.

Standard library only, as the rest of hostenv (hostctl imports it with any python3).

Config (environment):
  ANYTHINGLLM_URL  AnythingLLM's address (default http://127.0.0.1:3001)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any

log = logging.getLogger("hostenv.keys")

REFUSED = "No valid api key found."  # AnythingLLM's own words
REMEMBER = 60.0  # seconds a good key is remembered
TIMEOUT = 10
# For a route a client's web build calls with its key: any origin, the key in a header.
CLIENT_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization",
    "Access-Control-Allow-Methods": "GET, POST",
    "Access-Control-Max-Age": "600",
}

# None for a good key, else the status and error to answer with.
Check = Callable[[str], Awaitable[tuple[int, str] | None]]


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


async def refusal(check: Check, headers: dict[str, str]) -> tuple[str, str] | None:
    """The status line and error that refuse a request whose `Authorization: Bearer` key
    is missing or not good, or None once `check` has passed it."""
    scheme, _, key = headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not key.strip():
        return "401 Unauthorized", REFUSED
    if (refused := await check(key.strip())) is not None:
        status, error = refused
        return f"{status} {HTTPStatus(status).phrase}", error
    return None


class KeyCheck:
    """Asks AnythingLLM whether a developer API key is good."""

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
