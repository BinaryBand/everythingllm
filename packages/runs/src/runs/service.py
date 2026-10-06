"""The runs a host service holds for its callers, and the ops to follow them.

A service subclasses RunService, sets its class settings and starts a run from one of its
ops with `new_run` and `launch`, then answers at once with the run's id. The run's work is
a coroutine that gets the run, a `progress(message)` and a `meter(fraction)`, both safe to
call from a worker thread, and returns the run's result (a dict; "title" and "url" in it
are shown on the live card). The service then answers:

  wait(run_id, since=0)  up to WAIT seconds for news: {events (from `since` on), done,
                         result once done}
  runs()                 the runs it holds: {run_id, <SUBJECT_KEY>, started, done}

A run belongs to the service, not to a chat: it carries on when its caller goes away. At
most MAX_RUNS go at once; the rest wait their turn. A finished run can be fetched for
RESULT_KEEP seconds; the service's run log is the record after that. A caller waiting, or
a live card being watched, counts as someone following the run (RunService.followed).
"""

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import hostrpc
from hostrpc import RunnerError

from runs.runlog import MAX_EVENTS, sweep_interrupted

if TYPE_CHECKING:
    from runs.live import Live

Progress = Callable[[str], None]
Meter = Callable[[float], None]


@dataclass
class Run:
    id: str
    subject: str  # what it was asked to do: a question, a delegation's goal
    started: str
    events: list[str] = field(default_factory=list)
    fraction: float | None = None  # how far along, 0 to 1; None until it says
    title: str = ""  # the subject, then whatever the result calls itself
    url: str | None = None  # where its result is, once it's there
    began: float = field(default_factory=time.monotonic)
    done: bool = False
    result: dict | None = None
    finished: float = 0.0
    waiters: int = 0
    last_seen: float = field(default_factory=time.monotonic)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def minutes(self) -> int:
        """Whole minutes it has run (or ran), at least 1."""
        end = self.finished if self.done else time.monotonic()
        return max(1, round((end - self.began) / 60))


class RunService(hostrpc.Service):
    ID_PREFIX = "run-"  # then 8 hex digits
    NOUN = "run"  # as in "no research run 'x' here"
    SUBJECT_KEY = "subject"  # what runs() calls a run's subject
    MAX_RUNS = 2
    WAIT = 45  # a caller's wait is a long poll of this length
    RESULT_KEEP = 3600
    FOLLOW_GRACE = 15  # seconds a caller counts as following after it last looked

    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}
        self.slots = asyncio.Semaphore(self.MAX_RUNS)
        self.tasks: set[asyncio.Task] = set()
        self.live: asyncio.Server | None = (
            None  # its live cards' server, once it listens
        )

    def new_run(self, subject: str) -> Run:
        self.prune()
        return Run(
            f"{self.ID_PREFIX}{secrets.token_hex(4)}",
            subject,
            datetime.now(UTC).isoformat(timespec="seconds"),
            title=subject,
        )

    def followed(self, run: Run) -> bool:
        """Someone is waiting on the run or watching its card, or did a moment ago."""
        return run.waiters > 0 or time.monotonic() - run.last_seen < self.FOLLOW_GRACE

    def launch(
        self,
        run: Run,
        work: Callable[[Run, Progress, Meter], Awaitable[dict[str, Any]]],
    ) -> int:
        """Hold the run and start its work; returns how many runs it waits for first."""
        going = sum(1 for r in self.runs.values() if not r.done)
        self.runs[run.id] = run
        task = asyncio.create_task(self.go(run, work))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return max(0, going - (self.MAX_RUNS - 1))

    def crashed(self, error: Exception) -> dict[str, Any]:
        """The result of a run whose work raised: the service's own trouble."""
        return {"status": "failed", "error": f"The {self.NOUN} run failed: {error}."}

    async def go(
        self,
        run: Run,
        work: Callable[[Run, Progress, Meter], Awaitable[dict[str, Any]]],
    ) -> None:
        loop = asyncio.get_running_loop()

        def progress(message: str) -> None:
            if len(run.events) < MAX_EVENTS:  # as many as the run log keeps
                run.events.append(message)
                loop.call_soon_threadsafe(run.changed.set)

        def meter(fraction: float) -> None:
            # Parts of a run can report out of order; the bar only moves forward.
            if fraction > (run.fraction or 0):
                run.fraction = fraction
                loop.call_soon_threadsafe(run.changed.set)

        if self.slots.locked():
            progress(
                f"Waiting for one of the {self.MAX_RUNS} {self.NOUN} runs going now to finish first."
            )
        async with self.slots:
            try:
                result = await work(run, progress, meter)
            except Exception as e:  # the work reports its own failures; this is a crash
                self.log.exception("%s crashed", run.id)
                result = self.crashed(e)
        run.title = result.get("title") or run.title
        run.url = result.get("url")
        run.result, run.done, run.finished = result, True, time.monotonic()
        run.changed.set()
        loop.call_later(self.RESULT_KEEP + 1, self.prune)
        self.log.info("%s finished: %s", run.id, result.get("status"))

    async def op_wait(self, run_id: str, since: int = 0) -> dict:
        run = self.runs.get(run_id)
        if run is None:
            raise RunnerError(
                f"no {self.NOUN} run '{run_id}' here (finished over an hour ago, or the runner restarted)."
            )
        since = max(0, int(since))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.WAIT
        run.waiters += 1
        try:
            while len(run.events) <= since and not run.done:
                run.changed.clear()
                if len(run.events) > since or run.done:
                    break
                try:
                    await asyncio.wait_for(
                        run.changed.wait(), max(0, deadline - loop.time())
                    )
                except TimeoutError:
                    break
        finally:
            run.waiters -= 1
            run.last_seen = time.monotonic()
        return {
            "events": run.events[since:],
            "done": run.done,
            "result": run.result if run.done else None,
        }

    async def op_runs(self) -> dict:
        return {
            "runs": [
                {
                    "run_id": r.id,
                    self.SUBJECT_KEY: r.subject,
                    "started": r.started,
                    "done": r.done,
                }
                for r in self.runs.values()
            ]
        }

    async def serve(
        self,
        socket: Path,
        live: "Live",
        port: int,
        runlogs: Path,
        limit: int = hostrpc.LIMIT,
    ) -> None:
        """Log what an earlier service left running as interrupted, serve the live cards on
        `port` and answer on `socket` until stopped."""
        # Nothing in running/ can be ours yet: those runs died with an earlier service.
        for subject in sweep_interrupted(runlogs, everything=True):
            self.log.info(
                "logged a %s run an earlier runner left as interrupted: %s",
                self.NOUN,
                subject[:120],
            )
        # The live cards are a nicety: without their port, the runs still go.
        try:
            self.live = await live.serve(port)
        except OSError as e:
            self.log.error("no live cards: can't listen on port %s: %s", port, e)
        try:
            await hostrpc.serve(self, socket, limit=limit)
        finally:
            if self.live:
                self.live.close()
            await self.aclose()

    async def aclose(self) -> None:
        """Let go of what the service holds besides its runs, once it stops."""

    def prune(self) -> None:
        now = time.monotonic()
        for id in [
            id
            for id, r in self.runs.items()
            if r.done and now - r.finished > self.RESULT_KEEP
        ]:
            del self.runs[id]
