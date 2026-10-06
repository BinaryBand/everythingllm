"""Tells the Nilson app through ntfy that a run is done or failed, so it can open the chat.
The message is the start of the question; the answer's text never goes to ntfy.
"""

import logging
from typing import Any

import httpx

log = logging.getLogger("relay.notify")

QUESTION_CHARS = 120


def message(run: dict[str, Any], question: str) -> tuple[dict[str, str], bytes]:
    """The ntfy headers and body for a finished run."""
    headers = {
        "Title": "Answer ready" if run["status"] == "done" else "Answer failed",
        "Tags": f"run={run['id']},workspace={run['workspace']},thread={run['thread']}",
        "X-Relay-Run": run["id"],
        "X-Relay-Workspace": run["workspace"],
        "X-Relay-Thread": run["thread"],
    }
    return headers, " ".join(question.split())[:QUESTION_CHARS].encode()


def publisher(client: httpx.AsyncClient, url: str, token: str = ""):
    """A notify coroutine for relay.runs.Relay that posts to the ntfy topic at `url`."""

    async def publish(run: dict[str, Any], question: str) -> None:
        headers, body = message(run, question)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await client.post(url, content=body, headers=headers, timeout=10)
        except httpx.HTTPError as e:
            # Neither the topic's URL nor the token goes in the log: the topic is the secret.
            log.warning(
                "couldn't notify ntfy about %s: %s", run["id"], type(e).__name__
            )
            return
        if response.is_error:
            log.warning("ntfy answered %d for %s", response.status_code, run["id"])

    return publish
