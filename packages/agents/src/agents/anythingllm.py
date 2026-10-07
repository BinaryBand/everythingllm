"""Small clients for AnythingLLM's two APIs, as much of them as agents-runner needs.

`AnythingLLM` is the developer API (/api/v1), for delegation and telling a chat a job
ended: workspaces, threads and a thread's chat, which runs the workspace's agent headless
when the message starts with @agent. `InternalAPI` is the internal one (/api, which
AnythingLLM's UI uses), for its scheduled jobs, saved memories and threads' ids, which the
developer API doesn't have; it logs in with
AnythingLLM's password (hostrpc.anythingllm_headers), and once more after a 401.

Config (environment, from host.env and agents.env through the unit):
  ANYTHINGLLM_URL      AnythingLLM's address (default http://127.0.0.1:3001)
  ANYTHINGLLM_API_KEY  a developer API key, made in AnythingLLM's settings
  ANYTHINGLLM_ENV      AnythingLLM's .env, with the password the internal API logs in with
                       (default <ANYTHINGLLM_STORAGE>/.env)
"""

import asyncio
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import hostrpc
import httpx
from hostctl.jobs import job_tools

THINKING = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
TIMEOUT = httpx.Timeout(connect=10, read=30, write=30, pool=10)
CHAT_SECONDS = 600  # one task's agent run, at most


class AnythingLLMError(Exception):
    """AnythingLLM said no, or couldn't be reached; the text is for the caller."""


class NotFound(AnythingLLMError):
    """The internal API's 404: no such job, memory or workspace."""


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


async def send(
    http: httpx.AsyncClient, method: str, path: str, **kw: Any
) -> httpx.Response:
    """http.request, with a timeout or a connection failure as AnythingLLMError."""
    try:
        return await http.request(method, path, **kw)
    except httpx.TimeoutException:
        raise AnythingLLMError("AnythingLLM didn't answer in time.") from None
    except httpx.HTTPError as e:
        raise AnythingLLMError(f"couldn't reach AnythingLLM: {e}") from None


def decoded(res: httpx.Response) -> Any:
    """A reply's JSON, its text when it isn't JSON, or None when it's empty."""
    try:
        return res.json() if res.content.strip() else None
    except ValueError:
        return res.text


def without_thinking(text: str) -> str:
    """A reply without the model's <think>…</think>, which AnythingLLM passes through."""
    return THINKING.sub("", text or "").strip()


def tag_safe(text: str, tag: str) -> str:
    """`text` for inside a <tag>…</tag> in a prompt, which it can't close."""
    return re.sub(rf"<\s*/\s*({tag})", r"<\\/\1", text, flags=re.IGNORECASE)


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
                "~/.config/everythingllm/agents.env (uv run hostctl agents-setup checks it)."
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
        res = await send(self.http, method, path, json=body, timeout=timeout)
        if res.status_code >= 300:
            raise AnythingLLMError(status_error(res.status_code))
        return decoded(res)

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

    async def chat(
        self, slug: str, thread: str | None, message: str
    ) -> tuple[str, dict]:
        """The agent's reply to `message` in the thread (with None, the workspace's main
        chat), and the run's metrics (model, cost)."""
        path = f"/workspace/{quote(slug)}"
        if thread is not None:
            path += f"/thread/{quote(thread)}"
        reply = await self.call(
            "POST",
            f"{path}/chat",
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


def internal_error(status: int, reply: Any) -> str:
    """A plain-language message for a non-2xx answer from the internal API: AnythingLLM's
    own reason when it gave one (a 400's, or "Personalization is disabled."; a 5xx's as
    its error, not a refusal), else what the status means."""
    said = reply.get("error") if isinstance(reply, dict) else None
    if status == 401 or (status == 403 and not said):
        return "AnythingLLM refused agents-runner's login (the password in its .env)."
    if said and status >= 500:
        return f"AnythingLLM hit an error ({status}): {said}"
    if said:
        return f"AnythingLLM turned it down: {said}"
    if status == 404:
        return "AnythingLLM has no such item."
    return status_error(status)


def created(reply: Any, key: str, what: str) -> dict:
    """The `key` item of a 2xx reply that makes one, or an error saying AnythingLLM didn't
    `what`."""
    if not isinstance(reply, dict) or not reply.get(key):
        said = reply.get("error") if isinstance(reply, dict) else None
        raise AnythingLLMError(f"AnythingLLM didn't {what}: {said or reply}")
    return reply[key]


@dataclass
class InternalAPI:
    """AnythingLLM's internal API: its scheduled jobs, saved memories and threads' ids. `login(fresh)` gives the headers
    (by default hostrpc.anythingllm_headers, with the password in `env_file`); it runs in
    a thread, since it blocks, and once more with fresh=True after a 401."""

    base_url: str
    env_file: Path
    transport: httpx.AsyncBaseTransport | None = None  # tests
    login: Callable[[bool], dict[str, str]] | None = None  # tests
    http: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    @classmethod
    def from_env(cls) -> "InternalAPI":
        return cls(
            os.environ.get("ANYTHINGLLM_URL", "http://127.0.0.1:3001"),
            Path(os.environ.get("ANYTHINGLLM_ENV") or hostrpc.storage() / ".env"),
        )

    @property
    def api(self) -> str:
        return self.base_url.rstrip("/") + "/api"

    async def headers(self, fresh: bool = False) -> dict[str, str]:
        try:
            if self.login is not None:
                return await asyncio.to_thread(self.login, fresh)
            return await asyncio.to_thread(
                hostrpc.anythingllm_headers, self.api, self.env_file, fresh=fresh
            )
        except hostrpc.RunnerError as e:
            raise AnythingLLMError(str(e)) from None

    async def call(self, method: str, path: str, body: dict | None = None) -> Any:
        if self.http is None:
            self.http = httpx.AsyncClient(
                base_url=self.api, timeout=TIMEOUT, transport=self.transport
            )
        for fresh in (False, True):
            headers = await self.headers(fresh)
            res = await send(self.http, method, path, json=body, headers=headers)
            if res.status_code != 401:
                break
        reply = decoded(res)
        if res.status_code >= 300:
            error = NotFound if res.status_code == 404 else AnythingLLMError
            raise error(internal_error(res.status_code, reply))
        return reply

    async def aclose(self) -> None:
        if self.http is not None:
            await self.http.aclose()
            self.http = None

    async def jobs(self) -> list[dict]:
        """Every scheduled job, with its `tools` parsed."""
        found = (await self.call("GET", "/scheduled-jobs") or {}).get("jobs") or []
        return [parsed_tools(j) for j in found]

    async def job(self, job_id: int) -> dict:
        reply = await self.call("GET", f"/scheduled-jobs/{int(job_id)}") or {}
        if not reply.get("job"):
            raise AnythingLLMError(f"AnythingLLM has no scheduled job {job_id}.")
        return parsed_tools(reply["job"])

    async def runs(self, job_id: int) -> list[dict]:
        """A job's last 50 runs, newest first, without their results."""
        reply = await self.call("GET", f"/scheduled-jobs/{int(job_id)}/runs") or {}
        return [
            {k: v for k, v in r.items() if k != "result"}
            for r in reply.get("runs") or []
        ]

    async def available_tools(self) -> list[dict]:
        """What a job may be given: [{id, name, requiresSetup?}], from every category."""
        reply = await self.call("GET", "/scheduled-jobs/available-tools") or {}
        return [
            item
            for category in reply.get("tools") or []
            for item in category.get("items") or []
            if item.get("id")
        ]

    async def create(
        self, name: str, prompt: str, tools: list[str], schedule: str
    ) -> dict:
        reply = await self.call(
            "POST",
            "/scheduled-jobs/new",
            {"name": name, "prompt": prompt, "tools": tools, "schedule": schedule},
        )
        return created(reply, "job", "make the job")

    async def delete(self, job_id: int) -> None:
        await self.call("DELETE", f"/scheduled-jobs/{int(job_id)}")

    async def disable(self, job_id: int) -> None:
        await self.call("PUT", f"/scheduled-jobs/{int(job_id)}", {"enabled": False})

    async def memories(self, slug: str) -> dict[str, list[dict]]:
        """The saved memories for `slug`: {global: [...], workspace: [...]}, each newest
        first (a chat gets every global one and up to 5 of the workspace's)."""
        reply = (
            await self.call("GET", f"/workspaces/{quote(slug, safe='')}/memories") or {}
        )
        found = reply.get("memories") or {}
        return {k: found.get(k) or [] for k in ("global", "workspace")}

    async def memory_new(self, slug: str, content: str, scope: str) -> dict:
        reply = await self.call(
            "POST",
            f"/workspaces/{quote(slug, safe='')}/memories",
            {"content": content, "scope": scope},
        )
        return created(reply, "memory", "save it")

    async def memory_delete(self, memory_id: int) -> None:
        await self.call("DELETE", f"/memories/{int(memory_id)}")

    async def threads(self, slug: str) -> list[dict]:
        """The workspace's threads, with their ids (the developer API gives only slugs)."""
        reply = await self.call("GET", f"/workspace/{quote(slug, safe='')}/threads")
        return (reply or {}).get("threads") or []


def parsed_tools(job: dict) -> dict:
    """A job as the API gives it, with `tools` (JSON text there, or null) as a list or
    None; never its latest run's result, which can be long."""
    job = {**job, "tools": job_tools(job)}
    if isinstance(job.get("latestRun"), dict):
        job["latestRun"] = {k: v for k, v in job["latestRun"].items() if k != "result"}
    return job
