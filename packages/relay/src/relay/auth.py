"""Who may use the relay: anyone with an AnythingLLM developer API key that AnythingLLM
takes, so a client sends the relay the same key it sends AnythingLLM. The relay holds no key
of its own; it asks `GET /api/v1/auth` and remembers a key it took for a minute. A refused
key gets AnythingLLM's own answer, so to a client the relay's routes refuse a key as
AnythingLLM's do.

`RequireKey` puts the key in the request's state (`api_key`), and a run calls AnythingLLM
with it. The key is never logged, stored or put in an answer.
"""

import hashlib
import logging
import time

import httpx
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from relay.upstream import UNREACHABLE

log = logging.getLogger("relay.auth")

REFUSED = "No valid api key found."  # AnythingLLM's own words
UNCHECKED = "AnythingLLM couldn't check the API key ({status})."
REMEMBER_SECONDS = 60.0
TIMEOUT = httpx.Timeout(10)


class KeyCheck:
    """Asks AnythingLLM whether a key is good, remembering the good ones for a while (by
    their hash, so the cache holds no key)."""

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self.client = client
        self.url = base_url.rstrip("/") + "/api/v1/auth"
        self.good: dict[bytes, float] = {}

    async def __call__(self, key: str) -> tuple[int, str] | None:
        """None for a good key; otherwise the status and error to answer with."""
        digest = hashlib.sha256(key.encode()).digest()
        now = time.monotonic()
        if self.good.get(digest, 0) > now:
            return None
        try:
            response = await self.client.get(
                self.url, headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT
            )
        except httpx.TransportError as e:
            # The exception's text can carry the URL; the class says enough for the log.
            log.warning("checking a key failed: %s", type(e).__name__)
            return 502, UNREACHABLE
        if response.status_code in (401, 403):
            return 403, REFUSED
        if not 200 <= response.status_code < 300:
            log.warning("checking a key answered %d", response.status_code)
            return 502, UNCHECKED.format(status=response.status_code)
        self.good = {d: t for d, t in self.good.items() if t > now}
        self.good[digest] = now + REMEMBER_SECONDS
        return None


class RequireKey:
    """Answers 403 to any HTTP request but /health without `Authorization: Bearer <key>`
    for a key AnythingLLM takes, and 502 when AnythingLLM can't say. Plain ASGI, so streamed
    responses pass through untouched."""

    def __init__(self, app: ASGIApp, check: KeyCheck) -> None:
        self.app = app
        self.check = check

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/health":
            await self.app(scope, receive, send)
            return
        given = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
        scheme, _, key = given.partition(" ")
        key = key.strip()
        if scheme.lower() != "bearer" or not key:
            refused = 403, REFUSED
        else:
            refused = await self.check(key)
        if refused:
            status, message = refused
            await JSONResponse({"error": message}, status_code=status)(
                scope, receive, send
            )
            return
        scope.setdefault("state", {})["api_key"] = key
        await self.app(scope, receive, send)
