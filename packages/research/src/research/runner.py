"""research-runner: the host daemon that runs deep research for the AnythingLLM skill.

The skill (anythingllm/agent-skills/deep-research) asks over its socket (hostrpc):

  start(question, depth?, planner?, worker?, planner_fallback?, site?, embed?, workspace?,
        workspace_name?) -> {run_id, queued, card}
  wait(run_id, since=0)   up to WAIT seconds for news: {events (from `since` on), done,
                          result ({status, reply, sources}) once done}
  runs()                  the runs this runner holds: {run_id, question, started, done}

`card` is the run's live progress card for the agent to paste (research.live, which
this runner serves on its own port); "" without PUBLIC_HOST.

A run belongs to the runner, not to the chat: if the chat closes or AnythingLLM restarts,
it carries on, publishes and embeds as usual. At most MAX_RUNS go at once; the rest wait
their turn. Finished runs can be fetched for RESULT_KEEP seconds; the run log is the
record after that. Holding runs and waiting on them is runs.service's (RunService); this
runner adds `start`, which runs research.job.run in a thread.

Config (environment, from host.env and the unit):
  ANYTHINGLLM_STORAGE   storage directory (default /srv/anythingllm/storage)
  RESEARCH_SOCKET       socket to listen on (default <storage>/everythingllm/research/runner.sock)
  RESEARCH_LIVE_PORT    port on 127.0.0.1 for the live cards (default 8450; research.live)
  and what research.job.Settings reads.
"""

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import hostrpc
from hostrpc import RunnerError
from runs.runlog import sweep_interrupted
from runs.service import Meter, Progress, Run, RunService

from research import job, live

log = logging.getLogger("research-runner")


class Runner(RunService):
    log = log
    ID_PREFIX = "dr-"
    NOUN = "research"
    SUBJECT_KEY = "question"
    MAX_RUNS = 2

    def __init__(self, settings: job.Settings, execute=job.run):
        super().__init__()
        self.settings = settings
        self.execute = execute

    async def op_start(self, question: str, **args) -> dict:
        """args: depth, planner, worker, planner_fallback, site, embed, workspace,
        workspace_name (job.Request's fields); None or "" takes the default."""
        if not isinstance(question, str) or not question.strip():
            raise RunnerError("No research question was given.")
        req = job.Request.of(question, **args)
        run = self.new_run(req.question)
        card = live.card(self.settings.pages_url, run.id, req.question)
        req = replace(req, run_id=run.id, card=card)

        async def work(run: Run, progress: Progress, meter: Meter) -> dict[str, Any]:
            return await asyncio.to_thread(
                self.execute,
                req,
                self.settings,
                progress,
                lambda: not self.followed(run),
                meter,
            )

        queued = self.launch(run, work)
        log.info("%s started: %s", run.id, req.question[:120])
        # How many runs this one waits for before it can start.
        return {"run_id": run.id, "queued": queued, "card": card}

    def crashed(self, error: Exception) -> dict[str, Any]:
        # job.run doesn't raise; this is the runner's own trouble
        return {
            "status": "failed",
            "reply": f"The deep research run failed: {error}.",
            "sources": [],
        }


async def serve(
    settings: job.Settings, socket: Path, runner: Runner | None = None
) -> None:
    runner = runner or Runner(settings)
    # Nothing in running/ can be ours yet: those runs died with an earlier runner.
    for question in sweep_interrupted(settings.runlogs, everything=True):
        log.info(
            "logged a run an earlier runner left as interrupted: %s", question[:120]
        )
    # The live cards are a nicety: without their port, the runs still go.
    try:
        runner.live = await live.Live(runner).serve(settings.live_port)
    except OSError as e:
        log.error("no live cards: can't listen on port %s: %s", settings.live_port, e)
    try:
        await hostrpc.serve(runner, socket)
    finally:
        if runner.live:
            runner.live.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Hundreds of requests a run; the run log has what matters.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = job.Settings.from_env()
    asyncio.run(serve(settings, hostrpc.socket_path("research", "RESEARCH_SOCKET")))
