"""AnythingLLM's developer API (`/api/v1/...`) through the relay, so any AnythingLLM client
can use the relay's token as its API key: the request goes on as it came, with the relay's
token swapped for the developer API key, and the answer comes back as AnythingLLM gave it,
streamed (server-sent events too) and still encoded.

Only the hop-by-hop headers are dropped either way. A client that leaves stops the call to
AnythingLLM, as it would have without the relay; an answer that has to outlive its client
is a run (`POST /runs`).
"""

import asyncio
import logging

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from relay.upstream import TIMEOUT, UNREACHABLE

log = logging.getLogger("relay.proxy")

PREFIX = "/api/v1/"
# RFC 9110 7.6.1, plus what this hop sets itself.
HOP = {
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"proxy-connection",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
}
NOT_FORWARDED = HOP | {b"host", b"authorization"}


class Proxy:
    """An ASGI app for the routes under PREFIX."""

    def __init__(self, client: httpx.AsyncClient, base_url: str, api_key: str) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        headers = [(k, v) for k, v in scope["headers"] if k not in NOT_FORWARDED]
        headers.append((b"authorization", f"Bearer {self.api_key}".encode()))
        has_body = any(
            k in (b"content-length", b"transfer-encoding") for k, _ in scope["headers"]
        )
        path = scope.get("raw_path") or scope["path"].encode()
        url = self.base_url + path.decode("latin-1")
        if scope["query_string"]:
            url += "?" + scope["query_string"].decode("latin-1")
        # A bare Request, not client.build_request, which would add httpx's own
        # Accept-Encoding and User-Agent to what the client sent.
        outgoing = httpx.Request(
            scope["method"],
            url,
            headers=headers,
            content=request.stream() if has_body else None,
            extensions={"timeout": TIMEOUT.as_dict()},
        )
        try:
            response = await self.client.send(outgoing, stream=True)
        except httpx.TransportError as e:
            # The exception's text can carry the URL; the class says enough for the log.
            log.warning(
                "%s %s failed: %s", scope["method"], scope["path"], type(e).__name__
            )
            await JSONResponse({"error": UNREACHABLE}, status_code=502)(
                scope, receive, send
            )
            return
        try:
            await self.relay(response, receive, send)
        finally:
            await response.aclose()

    async def relay(
        self, response: httpx.Response, receive: Receive, send: Send
    ) -> None:
        """Streams the answer back until it ends or the client leaves, whichever is first.
        When AnythingLLM breaks off mid-answer, the response is left unfinished, so uvicorn
        drops the connection rather than pass a cut answer off as whole."""

        async def pump() -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": response.status_code,
                    "headers": [
                        (k, v) for k, v in response.headers.raw if k.lower() not in HOP
                    ],
                }
            )
            try:
                async for chunk in response.aiter_raw():
                    await send(
                        {"type": "http.response.body", "body": chunk, "more_body": True}
                    )
            except httpx.TransportError as e:
                log.warning(
                    "%s broke mid-answer: %s", response.url.path, type(e).__name__
                )
                return
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        async def left() -> None:
            while (await receive())["type"] != "http.disconnect":
                pass

        tasks = [asyncio.ensure_future(pump()), asyncio.ensure_future(left())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in done:
            task.result()
