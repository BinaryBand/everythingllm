"""Requests to turn a workspace's sandbox access on, from a chat that can't ask the user itself.

AnythingLLM's own window asks with its tool approval prompt (the sandbox-access skill). An API
chat, such as the Nilson app's through the relay, has no such prompt, so the skill leaves a
request here (the runner's op_access_ask) and a client holding the user's developer API key
shows it and answers it through the apps server (sandbox.appsweb). Nothing a skill can call
approves one: only appsweb, after checking the key, does (Runner.answer_access).

Requests live in memory: a restart loses them, and the agent asks again. A request is for one
change, applied as it was asked (never one the client sends), once."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sandbox.errors import SandboxError

ASK_SECONDS = 10 * 60  # how long a request waits for an answer
KEEP_ASKED = 24 * 3600  # how long an answered or expired one is still listed
MAX_WAITING = 10  # per workspace


class NoRequest(SandboxError):
    """The workspace has no such request."""


class NotWaiting(SandboxError):
    """The request was answered already, or has expired."""


@dataclass
class Request:
    id: str
    workspace: str
    thread: str
    change: dict[str, Any]  # what to apply: web, models, daily_tokens and/or heavy
    would: dict[str, Any]  # the access it would make, for the client to show
    text: str  # what the user is asked to approve, as AnythingLLM's prompt would say
    made: float
    state: str = "waiting"  # waiting, approved or declined (expired is worked out)
    answered: float | None = None


class AccessRequests:
    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self.now = now
        self.asked: dict[str, Request] = {}

    def state(self, r: Request) -> str:
        if r.state == "waiting" and r.made + ASK_SECONDS < self.now():
            return "expired"
        return r.state

    def prune(self) -> None:
        now = self.now()
        for r in [
            r
            for r in self.asked.values()
            if now - (r.answered or r.made + ASK_SECONDS) > KEEP_ASKED
        ]:
            del self.asked[r.id]

    def ask(
        self,
        workspace: str,
        thread: str,
        change: dict[str, Any],
        would: dict[str, Any],
        text: str,
    ) -> Request:
        """The workspace's waiting request for this change, or a new one."""
        self.prune()
        waiting = [
            r
            for r in self.asked.values()
            if r.workspace == workspace and self.state(r) == "waiting"
        ]
        if same := next((r for r in waiting if r.change == change), None):
            return same
        if len(waiting) >= MAX_WAITING:
            raise SandboxError(
                "this workspace has too many access requests waiting; the user has to "
                "answer them first"
            )
        r = Request(
            f"ar-{secrets.token_hex(16)}",
            workspace,
            thread,
            change,
            would,
            text,
            self.now(),
        )
        self.asked[r.id] = r
        return r

    def listing(self, workspace: str) -> list[Request]:
        """The workspace's requests, newest first."""
        return sorted(
            (r for r in self.asked.values() if r.workspace == workspace),
            key=lambda r: r.made,
            reverse=True,
        )

    def waiting(self, workspace: str, request_id: str) -> Request:
        """The workspace's request if it is still waiting; raises NoRequest, NotWaiting."""
        r = self.asked.get(request_id)
        if r is None or r.workspace != workspace:
            raise NoRequest("no such access request")
        if (state := self.state(r)) != "waiting":
            raise NotWaiting(f"that request is {state}")
        return r

    def answer(self, r: Request, state: str) -> None:
        r.state = state
        r.answered = self.now()

    def view(self, r: Request) -> dict[str, Any]:
        """The request as a client sees it."""
        return {
            "id": r.id,
            "state": self.state(r),
            "text": r.text,
            "would": r.would,
            "made": r.made,
            "expires": r.made + ASK_SECONDS,
        }
