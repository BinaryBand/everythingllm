"""research-runner: the host daemon that runs deep research for the AnythingLLM skill.

The skill (anythingllm/agent-skills/deep-research) asks over its socket (hostrpc):

  start(question, depth?, planner?, worker?, planner_fallback?, site?, embed?, workspace?,
        workspace_name?) -> {run_id, queued}
  wait(run_id, since=0)   up to WAIT seconds for news: {events (from `since` on), done,
                          result ({status, reply, sources}) once done}
  runs()                  the runs this runner holds: {run_id, question, started, done}

A run belongs to the runner, not to the chat: if the chat closes or AnythingLLM restarts,
it carries on, publishes and embeds as usual. At most MAX_RUNS go at once; the rest wait
their turn. Finished runs can be fetched for RESULT_KEEP seconds; the run log is the
record after that.

Config (environment, from host.env and the unit):
  ANYTHINGLLM_STORAGE   storage directory (default /srv/anythingllm/storage)
  RESEARCH_SOCKET       socket to listen on (default <storage>/research/runner.sock)
  and what research.job.Settings reads.
"""

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import hostrpc
from hostrpc import RunnerError

from research import job
from research.runlog import MAX_EVENTS, sweep_interrupted

log = logging.getLogger("research-runner")

MAX_RUNS = 2
WAIT = 45  # the skill's wait calls are long polls of this length
RESULT_KEEP = 3600
# The chat is counted as open while the skill is waiting, or waited this recently.
FOLLOW_GRACE = 15


@dataclass
class Run:
    id: str
    question: str
    started: str
    events: list[str] = field(default_factory=list)
    done: bool = False
    result: dict | None = None
    finished: float = 0.0
    waiters: int = 0
    last_seen: float = field(default_factory=time.monotonic)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def followed(self) -> bool:
        return self.waiters > 0 or time.monotonic() - self.last_seen < FOLLOW_GRACE


class Runner(hostrpc.Service):
    log = log

    def __init__(self, settings: job.Settings, execute=job.run):
        self.settings = settings
        self.execute = execute
        self.runs: dict[str, Run] = {}
        self.slots = asyncio.Semaphore(MAX_RUNS)
        self.tasks: set[asyncio.Task] = set()

    async def op_start(self, question: str, **args) -> dict:
        """args: depth, planner, worker, planner_fallback, site, embed, workspace,
        workspace_name (job.Request's fields); None or "" takes the default."""
        if not isinstance(question, str) or not question.strip():
            raise RunnerError("No research question was given.")
        req = job.Request.of(question, **args)
        self.prune()
        going = sum(1 for r in self.runs.values() if not r.done)
        run = Run(
            f"dr-{secrets.token_hex(4)}",
            req.question,
            datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self.runs[run.id] = run
        task = asyncio.create_task(self.go(run, req))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        log.info("%s started: %s", run.id, req.question[:120])
        # How many runs this one waits for before it can start.
        return {"run_id": run.id, "queued": max(0, going - (MAX_RUNS - 1))}

    async def go(self, run: Run, req: job.Request) -> None:
        loop = asyncio.get_running_loop()

        def progress(message: str) -> None:
            if len(run.events) < MAX_EVENTS:  # as many as the run log keeps
                run.events.append(message)
                loop.call_soon_threadsafe(run.changed.set)

        if self.slots.locked():
            progress(
                f"Waiting for one of the {MAX_RUNS} research runs going now to finish first."
            )
        async with self.slots:
            try:
                result = await asyncio.to_thread(
                    self.execute,
                    req,
                    self.settings,
                    progress,
                    lambda: not run.followed(),
                )
            except (
                Exception
            ) as e:  # job.run doesn't raise; this is the runner's own trouble
                log.exception("%s crashed", run.id)
                result = {
                    "status": "failed",
                    "reply": f"The deep research run failed: {e}.",
                    "sources": [],
                }
        run.result, run.done, run.finished = result, True, time.monotonic()
        run.changed.set()
        loop.call_later(RESULT_KEEP + 1, self.prune)
        log.info("%s finished: %s", run.id, result.get("status"))

    async def op_wait(self, run_id: str, since: int = 0) -> dict:
        run = self.runs.get(run_id)
        if run is None:
            raise RunnerError(
                f"no research run '{run_id}' here (finished over an hour ago, or the runner restarted)."
            )
        since = max(0, int(since))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + WAIT
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
                    "question": r.question,
                    "started": r.started,
                    "done": r.done,
                }
                for r in self.runs.values()
            ]
        }

    def prune(self) -> None:
        now = time.monotonic()
        for id in [
            id
            for id, r in self.runs.items()
            if r.done and now - r.finished > RESULT_KEEP
        ]:
            del self.runs[id]


async def serve(
    settings: job.Settings, socket: Path, runner: Runner | None = None
) -> None:
    runner = runner or Runner(settings)
    # Nothing in running/ can be ours yet: those runs died with an earlier runner.
    for question in sweep_interrupted(settings.runlogs, everything=True):
        log.info(
            "logged a run an earlier runner left as interrupted: %s", question[:120]
        )
    await hostrpc.serve(runner, socket)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Hundreds of requests a run; the run log has what matters.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = job.Settings.from_env()
    asyncio.run(serve(settings, hostrpc.socket_path("research", "RESEARCH_SOCKET")))
