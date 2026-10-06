"""Runs: each answer streams from AnythingLLM in its own task, which no follower owns, so
an app that closes, loses its network or is killed doesn't stop it. Followers read the
stored events and wait for new ones; any number can follow a run, and each can rejoin from
the last event it saw.

`Relay` is built around an `answer` function (relay.upstream's, in production; the tests
script their own) and an optional `notify` coroutine for finished runs.
"""

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import Any, Self

from relay.store import TERMINAL, Store, public

log = logging.getLogger("relay.runs")

RESTARTED = "The relay restarted during the answer."
CRASHED = "The relay hit an error during the answer."
PING_SECONDS = 15.0
PURGE_SECONDS = 3600.0

Event = tuple[str, dict[str, Any]]
# answer(workspace, thread, question, mode): the upstream answer's events.
Answer = Callable[[str, str, str, str], AsyncGenerator[Event]]
# notify(run, question): told once a run is done or failed.
Notify = Callable[[dict[str, Any], str], Awaitable[None]]


class Busy(Exception):
    """The thread already has a running run."""


def sse(seq: int, name: str, data: dict[str, Any]) -> str:
    return f"id: {seq}\nevent: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class Relay:
    def __init__(
        self,
        store: Store,
        answer: Answer,
        notify: Notify | None = None,
        retention_days: float = 7,
        ping: float = PING_SECONDS,
    ) -> None:
        self.store = store
        self.answer = answer
        self.notify = notify
        self.retention_days = retention_days
        self.ping = ping
        self.tasks: dict[str, asyncio.Task] = {}
        self.changed = asyncio.Condition()

    # --- lifetime ---

    async def __aenter__(self) -> Self:
        """Fail the runs a restart cut short, drop old ones, and keep dropping them hourly."""
        for row in self.store.runs("running"):
            self.store.append(row["id"], "failed", {"error": RESTARTED})
            log.info("run %s was cut short by a restart", row["id"])
        self.purge()
        self.purger = asyncio.create_task(self._purge_hourly())
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Stop every task without ending its run: the next start fails it as restarted."""
        tasks = [*self.tasks.values(), self.purger]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()

    def purge(self) -> None:
        if n := self.store.purge(self.retention_days):
            log.info("deleted %d runs older than %g days", n, self.retention_days)

    async def _purge_hourly(self) -> None:
        while True:
            await asyncio.sleep(PURGE_SECONDS)
            self.purge()

    # --- runs ---

    async def start(
        self, client_id: str, workspace: str, thread: str, question: str, mode: str
    ) -> tuple[dict[str, Any], bool]:
        """Start a run; returns it and whether it's new. A client id already used returns
        that run and starts nothing; a thread with a running run raises Busy."""
        if existing := self.store.by_client(client_id):
            return public(existing), False
        if self.store.running_on(workspace, thread):
            raise Busy
        run_id = "r_" + secrets.token_hex(8)
        row = self.store.create(run_id, client_id, workspace, thread, mode, question)
        self.tasks[run_id] = asyncio.create_task(
            self._run(run_id, workspace, thread, question, mode)
        )
        log.info("run %s started on %s/%s", run_id, workspace, thread)
        return public(row), True

    async def _run(
        self, run_id: str, workspace: str, thread: str, question: str, mode: str
    ) -> None:
        events = self.answer(workspace, thread, question, mode)
        try:
            async for name, data in events:
                await self._append(run_id, name, data)
                if name in TERMINAL:
                    break
            else:  # an answer that just stops is done (relay.upstream always ends itself)
                await self._append(run_id, "done", {"citations": []})
        except asyncio.CancelledError:
            raise  # cancel() or shutdown; they decide what the run becomes
        except Exception:
            log.exception("run %s failed in the relay", run_id)
            await self._append(run_id, "failed", {"error": CRASHED})
        finally:
            await events.aclose()  # closes the connection to AnythingLLM
            self.tasks.pop(run_id, None)
        row = self.store.get(run_id)
        if row is None:
            return
        log.info("run %s %s", run_id, row["status"])
        if self.notify is not None and row["status"] in ("done", "failed"):
            await self.notify(public(row), question)

    async def _append(self, run_id: str, name: str, data: dict[str, Any]) -> None:
        async with self.changed:
            self.store.append(run_id, name, data)
            self.changed.notify_all()

    async def cancel(self, run_id: str) -> dict[str, Any] | None:
        """Close the run's connection to AnythingLLM and end it `cancelled`; a run that has
        already ended is left as it is. None when there's no such run."""
        if (task := self.tasks.pop(run_id, None)) is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        row = self.store.get(run_id)
        if row is None:
            return None
        if row["status"] == "running":
            await self._append(run_id, "cancelled", {})
            log.info("run %s cancelled", run_id)
            row = self.store.get(run_id)
            assert row is not None
        return public(row)

    async def follow(self, run_id: str, after: int = 0) -> AsyncIterator[str]:
        """The run's events after `after` as server-sent events, then the live ones, with a
        ping comment while it's quiet; ends after the terminal event."""
        while True:
            async with self.changed:
                events = self.store.events(run_id, after)
                if not events:
                    row = self.store.get(run_id)
                    if row is None or row["status"] != "running":
                        return
                    try:
                        await asyncio.wait_for(self.changed.wait(), self.ping)
                        continue  # something changed; read it
                    except TimeoutError:
                        pass
            if not events:
                yield ": ping\n\n"
                continue
            for seq, name, data in events:
                yield sse(seq, name, data)
                after = seq
                if name in TERMINAL:
                    return
