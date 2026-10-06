"""One answer from AnythingLLM, as the relay's events: the call Nilson used to make itself,
`POST /api/v1/workspace/{slug}/thread/{thread}/stream-chat`, read to the end.

`answer()` posts Nilson's `stream-chat` body as it came, with the client's own API key, and
yields ("chunk", c) for each chunk AnythingLLM sends, unchanged and in order, then one
terminal event: ("done", {}) once the response ends, or ("failed", {"error"}). The relay doesn't interpret the answer: an
agent's thoughts, the closing chunk and the sources after it are all handed back. Closing
the generator closes the connection, which is how a run is cancelled; nothing else closes
it early.
"""

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger("relay.upstream")

ABORTED = "The server stopped the answer."
UNREACHABLE = "Couldn't reach AnythingLLM."
BROKEN = "The connection to AnythingLLM broke during the answer."
# A long pause mid-answer (a slow model, a tool call) is fine; ten silent minutes isn't.
TIMEOUT = httpx.Timeout(connect=10, read=600, write=30, pool=10)


def status_error(status: int) -> str:
    """A plain-language message for a non-2xx answer from AnythingLLM."""
    if status in (401, 403):
        return "AnythingLLM refused the API key."
    if status == 404:
        return "AnythingLLM doesn't know that workspace or thread."
    if status == 429:
        return "AnythingLLM is busy; try again in a moment."
    if status >= 500:
        return f"AnythingLLM hit an error ({status})."
    return f"AnythingLLM turned the question down ({status})."


def chunk(line: str) -> dict[str, Any] | None:
    """A `data:` line's JSON object; None for anything else."""
    if not line.startswith("data:"):
        return None
    try:
        value = json.loads(line[5:])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


async def answer(
    client: httpx.AsyncClient,
    base_url: str,
    workspace: str,
    thread: str,
    body: dict[str, Any],
    api_key: str,
) -> AsyncGenerator[tuple[str, dict[str, Any]]]:
    url = (
        f"{base_url.rstrip('/')}/api/v1/workspace/{quote(workspace, safe='')}"
        f"/thread/{quote(thread, safe='')}/stream-chat"
    )
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "text/event-stream"}
    started = False
    try:
        async with client.stream(
            "POST",
            url,
            json=body,
            headers=headers,
            timeout=TIMEOUT,
        ) as response:
            started = True
            if not 200 <= response.status_code < 300:
                log.warning("stream-chat answered %d", response.status_code)
                yield "failed", {"error": status_error(response.status_code)}
                return
            async for line in response.aiter_lines():
                if (c := chunk(line)) is None:
                    continue
                if isinstance(error := c.get("error"), str) and error.strip():
                    yield "failed", {"error": error.strip()}
                    return
                if c.get("type") == "abort":
                    yield "failed", {"error": ABORTED}
                    return
                yield "chunk", c
    except httpx.TransportError as e:
        # The exception's text can carry the URL; the class says enough for the log.
        log.warning("stream-chat failed: %s", type(e).__name__)
        yield "failed", {"error": BROKEN if started else UNREACHABLE}
        return
    yield "done", {}
