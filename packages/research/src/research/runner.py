"""research-runner: the host daemon that runs deep research for the AnythingLLM skill.

The skill (anythingllm/agent-skills/deep-research) asks over its socket (hostrpc):

  start(question, depth?, planner?, worker?, planner_fallback?, site?, embed?, workspace?,
        workspace_name?, sub_questions?, title?) -> {run_id, queued, card}

`sub_questions` is the calling agent's own split of the question (each a goal, or {goal,
queries}), which the planner then doesn't make; `title` is the report's title with them.
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
from runs.service import Meter, Progress, Run, RunService

from research import job, live

log = logging.getLogger("research-runner")

MAX_SUB_QUESTIONS = 8  # the most workers a depth has
MAX_GOAL = 500
MAX_TITLE = 120


def check_split(sub_questions: Any, title: Any) -> None:
    """RunnerError unless the caller's sub_questions and title are usable."""
    if sub_questions is not None:
        if not isinstance(sub_questions, list) or not (
            1 <= len(sub_questions) <= MAX_SUB_QUESTIONS
        ):
            raise RunnerError(
                f"sub_questions must be a list of 1 to {MAX_SUB_QUESTIONS} parts of the question."
            )
        for i, part in enumerate(sub_questions, 1):
            goal = part.get("goal") if isinstance(part, dict) else part
            if not isinstance(goal, str) or not goal.strip():
                raise RunnerError(
                    f"sub_questions[{i}] must be a goal, or an object with a goal."
                )
            if len(goal) > MAX_GOAL:
                raise RunnerError(f"sub_questions[{i}] is over {MAX_GOAL} characters.")
    if title is not None and (not isinstance(title, str) or len(title) > MAX_TITLE):
        raise RunnerError(f"title must be text of at most {MAX_TITLE} characters.")


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
        workspace_name, sub_questions, title (job.Request's fields); None or "" takes the
        default."""
        if not isinstance(question, str) or not question.strip():
            raise RunnerError("No research question was given.")
        check_split(args.get("sub_questions"), args.get("title") or None)
        req = job.Request.of(question, **args)
        run = self.new_run(req.question)
        card = live.Live.card_line(self.settings.pages_url, run.id, req.question)
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
    card = live.Live(runner, settings.runlogs, settings.pages_url)
    await runner.serve(socket, card, settings.live_port, settings.runlogs)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Hundreds of requests a run; the run log has what matters.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = job.Settings.from_env()
    asyncio.run(serve(settings, hostrpc.socket_path("research", "RESEARCH_SOCKET")))
