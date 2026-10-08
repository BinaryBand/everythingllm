"""Model access for a sandbox run: a model the run's code can ask, with no key in the run.

A workspace the user gave model access (the sandbox-access skill; runner.Access) gets, for
each run, a socket of the run's own, served here in sandbox-runner and mounted into the
container (runner.MODELS_DIR). The socket says who is calling: one run, of one workspace
and thread, so nothing a request says about that is trusted. Its one op, `ask`, sends the
messages to a model of ALLOWED through packages/llm, with the key from AnythingLLM's .env,
read here on the host; the run never sees it. The code in the run asks with the stdlib
client runner copies into its /sandbox (model_client.py, as everythingllm_models.py).

A workspace has a budget of tokens a day, in and out, in the user's time zone (its Access's
daily_tokens, DAILY_TOKENS by default): a call is refused once the day's calls have used it.
Every call is logged, as one line in <log dir>/YYYY-MM.jsonl: when, the workspace and thread,
the model and the tokens; never what was asked or answered.

Config (environment, from host.env through the unit):
  USER_TIMEZONE  the user's time zone, whose day the budget is (default Europe/Stockholm)
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import hostrpc
import llm
from hostrpc import RunnerError

log = logging.getLogger("sandbox.models")

DEFAULT_MODEL = "deepseek-flash"
ALLOWED = ("deepseek-flash", "glm-5.3")
DAILY_TOKENS = 200_000
MAX_TOKENS = 8192  # the most one answer may have
DEFAULT_MAX_TOKENS = 2048
MAX_CHARS = 200_000  # the most text one request may send
AT_ONCE = 4  # calls a run may have going at once
ROLES = ("system", "user", "assistant")

# (model, messages, max_tokens) -> (text, usage)
Ask = Callable[[str, list[dict], int], tuple[str, dict]]


def provider_ask(env_file: Path) -> Ask:
    """Ask a model through packages/llm, its key from AnythingLLM's .env at `env_file`
    (read on every call, so a new key is used at once; a client is kept per key)."""
    clients: dict[tuple[str, str, str], llm.Completions] = {}

    def ask(model: str, messages: list[dict], max_tokens: int) -> tuple[str, dict]:
        prov = llm.provider(llm.provider_for(model), str(env_file))
        if not prov.key:
            raise RunnerError(f"there's no {prov.key_name} for {model} on the server")
        key = (prov.name, prov.base_url, prov.key)
        if key not in clients:
            clients[key] = llm.Completions(prov, timeout=300, retries=1)
        try:
            # DeepSeek's thinking only costs tokens here; GLM can't turn it off.
            return clients[key].create(
                model, messages, max_tokens, think=prov.name == "zai"
            )
        except llm.LLMError as e:
            raise RunnerError(str(e)) from None

    return ask


def spent(log_dir: Path, workspace: str, now: datetime) -> int:
    """The tokens the workspace's calls have used on `now`'s day."""
    day = now.date().isoformat()
    file = log_dir / f"{now:%Y-%m}.jsonl"
    total = 0
    try:
        lines = file.read_text().splitlines()
    except FileNotFoundError:
        return 0
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("workspace") == workspace and entry.get("day") == day:
            total += int(entry.get("tokens") or 0)
    return total


def checked(messages: Any) -> list[dict]:
    """`messages` as the API takes them; RunnerError unless they're usable."""
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    if not isinstance(messages, list) or not messages:
        raise RunnerError("messages must be a prompt, or a list of {role, content}")
    out, chars = [], 0
    for m in messages:
        if (
            not isinstance(m, dict)
            or m.get("role") not in ROLES
            or not isinstance(m.get("content"), str)
        ):
            raise RunnerError(
                f"each message must be {{role, content}}, role one of {', '.join(ROLES)}"
            )
        chars += len(m["content"])
        out.append({"role": m["role"], "content": m["content"]})
    if chars > MAX_CHARS:
        raise RunnerError(f"the messages are over {MAX_CHARS} characters")
    return out


class Models(hostrpc.Service):
    """One run's model access: `ask`, for its workspace and thread alone."""

    log = log

    def __init__(
        self,
        workspace: str,
        thread: str,
        daily_tokens: int,
        log_dir: Path,
        ask: Ask,
        now: Callable[[], datetime] | None = None,
    ):
        super().__init__()
        self.workspace = workspace
        self.thread = thread
        self.daily_tokens = daily_tokens
        self.log_dir = log_dir
        self.ask = ask
        self.now = now or (lambda: datetime.now(hostrpc.user_zone()))
        self.at_once = asyncio.Semaphore(AT_ONCE)
        self.lock = asyncio.Lock()  # one writer of the log at a time
        # The day's tokens so far: read from the log once a day, then counted here (a
        # run holds its workspace, so no other run's calls add to it meanwhile).
        self.used: tuple[str, int] | None = None

    def left(self) -> int:
        now = self.now()
        day = now.date().isoformat()
        if self.used is None or self.used[0] != day:
            self.used = (day, spent(self.log_dir, self.workspace, now))
        return max(0, self.daily_tokens - self.used[1])

    async def op_ask(
        self, messages: Any, model: Any = None, max_tokens: Any = None
    ) -> dict[str, Any]:
        model = model or DEFAULT_MODEL
        if model not in ALLOWED:
            raise RunnerError(f"model must be one of: {', '.join(ALLOWED)}")
        max_tokens = DEFAULT_MAX_TOKENS if max_tokens is None else max_tokens
        if (
            not isinstance(max_tokens, int)
            or isinstance(max_tokens, bool)
            or not 1 <= max_tokens <= MAX_TOKENS
        ):
            raise RunnerError(f"max_tokens must be 1 to {MAX_TOKENS}")
        messages = checked(messages)
        async with self.at_once:
            if await asyncio.to_thread(self.left) <= 0:
                raise RunnerError(
                    f"this workspace has used its {self.daily_tokens} model tokens for "
                    "today; more tomorrow"
                )
            text, usage = await asyncio.to_thread(self.ask, model, messages, max_tokens)
            tokens = int(usage.get("prompt_tokens") or 0) + int(
                usage.get("completion_tokens") or 0
            )
            async with self.lock:
                await asyncio.to_thread(self.record, model, tokens)
            left = await asyncio.to_thread(self.left)
        return {"text": text, "model": model, "tokens": tokens, "tokens_left": left}

    def record(self, model: str, tokens: int) -> None:
        now = self.now()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        line = {
            "time": now.isoformat(timespec="seconds"),
            "day": now.date().isoformat(),
            "workspace": self.workspace,
            "thread": self.thread,
            "model": model,
            "tokens": tokens,
        }
        with open(self.log_dir / f"{now:%Y-%m}.jsonl", "a") as f:
            f.write(json.dumps(line) + "\n")
        if self.used is not None and self.used[0] == line["day"]:
            self.used = (line["day"], self.used[1] + tokens)
