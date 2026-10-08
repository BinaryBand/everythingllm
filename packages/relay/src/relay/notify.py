"""Tells the Nilson app through ntfy that a run is done or failed, so it can open the chat.
The message is the start of the question; the answer's text never goes to ntfy.
"""

import logging
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger("relay.notify")

QUESTION_CHARS = 120


def message(run: dict[str, Any], question: str) -> tuple[dict[str, str], bytes]:
    """The ntfy headers and body for a finished run."""
    headers = {
        "Title": "Answer ready" if run["status"] == "done" else "Answer failed",
        # Subscribers get the tags, not other request headers. The client names the
        # workspace and thread: quoted, a comma can't add a tag, nor a non-ASCII
        # character make the header unsendable.
        "Tags": ",".join(
            f"{tag}={quote(str(run[key]), safe='')}"
            for tag, key in (
                ("run", "id"),
                ("workspace", "workspace"),
                ("thread", "thread"),
            )
        ),
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
        except Exception as e:  # noqa: BLE001 - a notice that can't go is only logged
            # Neither the topic's URL nor the token goes in the log: the topic is the secret.
            log.warning(
                "couldn't notify ntfy about %s: %s", run["id"], type(e).__name__
            )
            return
        if response.is_error:
            log.warning("ntfy answered %d for %s", response.status_code, run["id"])

    return publish
