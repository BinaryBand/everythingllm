"""A small client for AnythingLLM's developer API (/api/v1), as much of it as delegation
needs: workspaces, threads and a thread's chat, which runs the workspace's agent headless
when the message starts with @agent.

Config (environment, from host.env and agents.env through the unit):
  ANYTHINGLLM_URL      AnythingLLM's address (default http://127.0.0.1:3001)
  ANYTHINGLLM_API_KEY  a developer API key, made in AnythingLLM's settings
"""

import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

THINKING = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
TIMEOUT = httpx.Timeout(connect=10, read=30, write=30, pool=10)
CHAT_SECONDS = 600  # one task's agent run, at most


class AnythingLLMError(Exception):
    """AnythingLLM said no, or couldn't be reached; the text is for the caller."""


def status_error(status: int) -> str:
    """A plain-language message for a non-2xx answer (as the relay's upstream.status_error)."""
    if status in (401, 403):
        return "AnythingLLM refused the delegation's API key."
    if status == 404:
        return "AnythingLLM doesn't know that workspace or thread."
    if status == 429:
        return "AnythingLLM is busy; try again in a moment."
    if status >= 500:
        return f"AnythingLLM hit an error ({status})."
    return f"AnythingLLM turned the request down ({status})."


def without_thinking(text: str) -> str:
    """A reply without the model's <think>…</think>, which AnythingLLM passes through."""
    return THINKING.sub("", text or "").strip()


@dataclass
class AnythingLLM:
    base_url: str
    api_key: str
    transport: httpx.AsyncBaseTransport | None = None  # tests
    http: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    @classmethod
    def from_env(cls) -> "AnythingLLM":
        key = os.environ.get("ANYTHINGLLM_API_KEY", "")
        if not key:
            raise AnythingLLMError(
                "no ANYTHINGLLM_API_KEY: make one in AnythingLLM's settings and put it in "
                "~/.config/everythingllm/agents.env (make agents-setup checks it)."
            )
        return cls(os.environ.get("ANYTHINGLLM_URL", "http://127.0.0.1:3001"), key)

    async def call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        seconds: float | None = None,
    ) -> Any:
        if self.http is None:  # one client, so its connections are reused
            self.http = httpx.AsyncClient(
                base_url=self.base_url.rstrip("/") + "/api/v1",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=TIMEOUT,
                transport=self.transport,
            )
        timeout = (
            httpx.USE_CLIENT_DEFAULT
            if seconds is None
            else httpx.Timeout(seconds, connect=10)
        )
        try:
            res = await self.http.request(method, path, json=body, timeout=timeout)
        except httpx.TimeoutException:
            raise AnythingLLMError("AnythingLLM didn't answer in time.") from None
        except httpx.HTTPError as e:
            raise AnythingLLMError(f"couldn't reach AnythingLLM: {e}") from None
        if res.status_code >= 300:
            raise AnythingLLMError(status_error(res.status_code))
        try:
            return res.json() if res.content.strip() else None
        except ValueError:
            return res.text

    async def aclose(self) -> None:
        if self.http is not None:
            await self.http.aclose()
            self.http = None

    async def workspaces(self) -> list[dict]:
        return (await self.call("GET", "/workspaces") or {}).get("workspaces", [])

    async def workspace_new(self, name: str) -> dict:
        return (await self.call("POST", "/workspace/new", {"name": name}))["workspace"]

    async def workspace_update(self, slug: str, settings: dict) -> None:
        await self.call("POST", f"/workspace/{quote(slug)}/update", settings)

    async def thread_new(self, slug: str, name: str) -> str:
        reply = await self.call(
            "POST", f"/workspace/{quote(slug)}/thread/new", {"name": name}
        )
        return reply["thread"]["slug"]

    async def thread_delete(self, slug: str, thread: str) -> None:
        await self.call("DELETE", f"/workspace/{quote(slug)}/thread/{quote(thread)}")

    async def chat(self, slug: str, thread: str, message: str) -> tuple[str, dict]:
        """The agent's reply to `message` in the thread, and the run's metrics (model, cost)."""
        reply = await self.call(
            "POST",
            f"/workspace/{quote(slug)}/thread/{quote(thread)}/chat",
            {"message": message, "mode": "chat"},
            seconds=CHAT_SECONDS,
        )
        if not isinstance(reply, dict):
            raise AnythingLLMError("AnythingLLM's answer wasn't JSON.")
        if reply.get("error") and not reply.get("textResponse"):
            raise AnythingLLMError(f"the agent failed: {reply['error']}")
        return without_thinking(reply.get("textResponse") or ""), reply.get(
            "metrics"
        ) or {}
