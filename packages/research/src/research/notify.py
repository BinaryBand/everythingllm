"""Tells the Nilson app through ntfy that a research run from one of its chats ended, on the
topic the relay posts finished answers to (relay.notify), so the app can bring the report
into the chat that started it. The message is the start of the question and a link to the
report; the report's text never goes to ntfy.

Config (environment; the container gets them from ~/.config/everythingllm/relay.env, the
relay's file, which hostctl.relay_env makes):
  NTFY_URL, NTFY_TOKEN  the ntfy topic and its token; no notices without NTFY_URL
"""

import logging
import os
from typing import Any

import httpx

log = logging.getLogger("research.notify")

QUESTION_CHARS = 120


def message(
    run_id: str, ok: bool, question: str, scope: dict[str, str], url: str | None
) -> tuple[dict[str, str], bytes]:
    """The ntfy headers and body for an ended run. `scope` is the chat's (_lib/scope.js):
    its workspace's slug and its thread's id."""
    headers = {
        "Title": "Research ready" if ok else "Research failed",
        # Subscribers get the tags, not other request headers.
        "Tags": f"run={run_id},workspace={scope['workspace']},thread={scope['thread']}",
    }
    if url:
        headers["Click"] = url
    return headers, " ".join(question.split())[:QUESTION_CHARS].encode()


def publisher(client: httpx.AsyncClient, url: str, token: str = ""):
    """A notify coroutine for research.runner.Runner that posts to the topic at `url`."""

    async def publish(
        run_id: str, ok: bool, question: str, scope: dict[str, str], report: str | None
    ) -> None:
        headers, body = message(run_id, ok, question, scope, report)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await client.post(url, content=body, headers=headers, timeout=10)
        except httpx.HTTPError as e:
            # Neither the topic's URL nor the token goes in the log: the topic is the secret.
            log.warning("couldn't notify ntfy about %s: %s", run_id, type(e).__name__)
            return
        if response.is_error:
            log.warning("ntfy answered %d for %s", response.status_code, run_id)

    return publish


def from_env():
    """The publisher for NTFY_URL and NTFY_TOKEN, or None without NTFY_URL. Its client goes
    out through HTTPS_PROXY, the egress proxy, as httpx's does by default."""
    url = os.environ.get("NTFY_URL", "").strip()
    if not url:
        return None
    return publisher(httpx.AsyncClient(), url, os.environ.get("NTFY_TOKEN", "").strip())
