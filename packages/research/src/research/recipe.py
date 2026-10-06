"""The agents engine: deep research as a recipe of delegations to agents-runner
(packages/agents), beside the pipeline (research.pipeline), which it mirrors step by step.

  1. plan       an agents-planner task splits the question into sub-questions
  2. workers    an agents-worker task per sub-question searches and reads with AnythingLLM's
                own web tools, and ends with findings: [{claim, quote, url}]
  3. checks     in code: a finding is kept only when its quote is on its page, fetched here
                (research.web.make_checker), and its source is numbered here (SourceList)
  4. gaps       a planner task reviews the notes; its follow-ups go to more workers
  5. write      a planner task writes the report from the checked notes only
  6. fact-check a planner task's edits, applied by pipeline.apply_edits
  7. finish     finalize_citations, in code

The planner's tasks are plain chats (`tools: false`), with what they work on passed as
material, not instructions. It returns the report the pipeline does, so research.job
publishes it the same way. It gives up the pipeline's run-wide search budget and pacing:
searches go through AnythingLLM's tool, and a worker is held to its own tool-call limit.
"""

import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import hostrpc
from hostrpc import RunnerError
from llm import parse_json

from research import config as cfg
from research import prompts
from research.pipeline import (
    apply_edits,
    clean,
    notes_by_source,
    notes_by_task,
    notes_report,
    tasks_from,
)
from research.sources import SourceList, finalize_citations, summary_bullets
from research.web import Check

# delegate(goal, tasks, (meter from, meter to)) -> agents-runner's result, with its run_id
Delegate = Callable[[str, list[dict], tuple[float, float]], dict]

MAX_GOAL = 2000  # agents-runner's
CALL_SECONDS = 60  # a wait is a long poll of 45 s


@dataclass
class Context:
    delegate: Delegate
    check: Check
    progress: Callable[[str], None]
    today: str  # YYYY-MM-DD
    meter: Callable[[float], None] = lambda _: None


@dataclass
class Spent:
    """What the run's delegations used, for the stats."""

    delegations: list[str] = field(default_factory=list)
    cost: float = 0.0
    tokens: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, result: dict) -> None:
        if result.get("run_id"):
            self.delegations.append(result["run_id"])
        self.cost += float(result.get("cost") or 0)
        for model, used in (result.get("tokens") or {}).items():
            mine = self.tokens.setdefault(model, {"prompt": 0, "completion": 0})
            for k in mine:
                mine[k] += int(used.get(k) or 0)


class AgentsRunner:
    """A Delegate over agents-runner's socket, for blocking code: starts the delegation,
    follows it (its progress lines go to `progress`, its meter to `meter` within the step's
    span) and cancels it if following fails."""

    def __init__(
        self,
        socket: Path,
        progress: Callable[[str], None],
        meter: Callable[[float], None],
        request: Callable[..., Any] = hostrpc.request_sync,
    ):
        self.socket = socket
        self.progress = progress
        self.meter = meter
        self.request = request

    def call(self, op: str, args: dict) -> Any:
        return self.request(self.socket, op, args, CALL_SECONDS, name="agents runner")

    def __call__(self, goal: str, tasks: list[dict], span: tuple[float, float]) -> dict:
        try:
            started = self.call("delegate", {"goal": goal, "tasks": tasks})
        except RunnerError as e:
            if "isn't running" in str(e):
                raise RuntimeError(f"{e} It needs `make agents-setup`.") from None
            raise
        run_id = started["run_id"]
        since, done = 0, False
        try:
            while True:
                reply = self.call("wait", {"run_id": run_id, "since": since})
                for event in reply["events"]:
                    self.progress(f"[agents] {event}")
                since += len(reply["events"])
                if reply.get("fraction") is not None:
                    self.meter(span[0] + (span[1] - span[0]) * reply["fraction"])
                if reply["done"]:
                    done = True
                    return {**reply["result"], "run_id": run_id}
        finally:
            if not done:
                try:
                    self.call("cancel", {"run_id": run_id})
                except RunnerError:
                    pass


FENCE = re.compile(r"```(?:json)?[ \t]*\n(.*?)```", re.DOTALL)


def reply_json(text: str) -> dict:
    """The JSON object an agent ended its reply with: its last ```json block, else the
    first object in the reply; ValueError if there's none."""
    for block in reversed(FENCE.findall(text)):
        try:
            out = json.loads(block)
        except ValueError:
            continue
        if isinstance(out, dict):
            return out
    return parse_json(text)


def as_task(messages: list[dict]) -> tuple[str, str]:
    """A pipeline prompt as a delegated task: its system message is the instructions, the
    rest (what to work on) the material."""
    system = [m["content"] for m in messages if m["role"] == "system"]
    rest = [m["content"] for m in messages if m["role"] != "system"]
    return "\n\n".join(system), "\n\n".join(rest)


def outcome_of(result: dict, name: str) -> dict:
    """A task's outcome in a delegation's result; a failed delegation's tasks failed with it."""
    for o in result.get("tasks") or []:
        if o.get("name") == name:
            return o
    return {
        "name": name,
        "status": "failed",
        "error": result.get("error") or "the delegation failed",
    }


def research(question: str, depth: str | None, ctx: Context) -> dict:
    """Research a question through delegations and return a finished, cited Markdown
    report, as pipeline.research does."""
    progress = ctx.progress
    question = clean(question)
    if not question:
        raise ValueError("No research question was given.")
    preset = cfg.depth_preset(depth)
    started = time.monotonic()
    goal = f"Deep research on: {question}"[:MAX_GOAL]
    sources = SourceList()
    spent = Spent()
    dropped = 0

    def judge(name: str, messages: list[dict], span: tuple[float, float]) -> str:
        """One planner task over what it's given; its reply, or RuntimeError."""
        instructions, material = as_task(messages)
        result = ctx.delegate(
            goal,
            [
                {
                    "name": name,
                    "profile": "planner",
                    "instructions": instructions,
                    "material": material,
                    "tools": False,
                }
            ],
            span,
        )
        spent.add(result)
        outcome = outcome_of(result, name)
        if outcome.get("status") != "ok":
            raise RuntimeError(outcome.get("error") or outcome.get("status"))
        return outcome.get("text") or ""

    def run_round(
        batch: list[dict], label: str, span: tuple[float, float]
    ) -> list[dict]:
        nonlocal dropped
        names = [f"{label}{i + 1}" for i in range(len(batch))]
        result = ctx.delegate(
            goal,
            [
                {
                    "name": name,
                    "profile": "worker",
                    "instructions": prompts.agent_worker(
                        question,
                        task["goal"],
                        task["queries"],
                        ctx.today,
                        preset["steps"],
                        cfg.NOTES_PER_WORKER,
                    ),
                }
                for name, task in zip(names, batch, strict=True)
            ],
            span,
        )
        spent.add(result)
        replies = []
        for name, task in zip(names, batch, strict=True):
            outcome = outcome_of(result, name)
            found: list[dict] = []
            summary = ""
            if outcome.get("status") != "ok":
                stopped = "failed"
                progress(f"Worker {name} failed: {outcome.get('error')}")
            else:
                try:
                    out = reply_json(outcome.get("text") or "")
                    stopped = "done"
                    summary = clean(out.get("summary"))
                    found = [
                        f for f in out.get("findings") or [] if isinstance(f, dict)
                    ]
                except ValueError:
                    stopped = "no-json"
                    progress(f"Worker {name} ended without its findings as JSON.")
            replies.append(
                (name, task, outcome, found[: cfg.NOTES_PER_WORKER], summary, stopped)
            )

        # Every quote against its page: the pages are fetched once each, a few at a time.
        claims = [
            (i, clean(f.get("claim")), clean(f.get("quote")), clean(f.get("url")))
            for i, reply in enumerate(replies)
            for f in reply[3]
        ]
        with ThreadPoolExecutor(max_workers=cfg.LIMITS["fetch"]) as pool:
            titles = list(
                pool.map(
                    lambda c: (
                        ctx.check(c[3], c[2])
                        if c[1] and re.match(r"^https?://", c[3])
                        else None
                    ),
                    claims,
                )
            )
        notes_of: dict[int, list[dict]] = {i: [] for i in range(len(replies))}
        dropped_of: dict[int, int] = {i: 0 for i in range(len(replies))}
        for (i, claim, quote, url), title in zip(claims, titles, strict=True):
            if title is None:
                dropped_of[i] += 1
                continue
            source = sources.add(url, title)
            notes_of[i].append(
                {
                    "claim": claim,
                    "quote": quote,
                    "source_id": source["id"],
                    "goal": replies[i][1]["goal"],
                }
            )
        done = []
        for i, (name, task, outcome, found, summary, stopped) in enumerate(replies):
            dropped += dropped_of[i]
            if found:
                extra = (
                    f", {dropped_of[i]} dropped (quote not on its page)"
                    if dropped_of[i]
                    else ""
                )
                progress(f"[{name}] {len(notes_of[i])} findings kept{extra}")
            done.append(
                {
                    "goal": task["goal"],
                    "notes": notes_of[i],
                    "summary": summary,
                    "detail": {
                        "goal": task["goal"],
                        "findings": len(notes_of[i]),
                        "dropped": dropped_of[i],
                        "stopped": stopped,
                        "seconds": outcome.get("seconds"),
                        "model": outcome.get("model"),
                        "tokens": outcome.get("tokens"),
                    },
                }
            )
        return done

    ctx.meter(0.02)
    progress(
        f"Planning {preset['name']} research through agents: {preset['workers']} workers in parallel."
    )
    try:
        plan = reply_json(
            judge(
                "plan",
                prompts.plan(question, ctx.today, preset["workers"]),
                (0.02, 0.08),
            )
        )
    except ValueError as e:
        raise RuntimeError(f"The planner's reply wasn't the plan: {e}") from None
    title = clean(plan.get("title"))[:120] or question[:120]
    tasks = tasks_from(plan.get("sub_questions"), preset["workers"])
    if not tasks:
        raise RuntimeError("The planner returned no sub-questions.")
    progress("Plan: " + " ".join(f"{i + 1}) {t['goal']}" for i, t in enumerate(tasks)))

    results = run_round(tasks, "w", (0.08, 0.6))
    rounds = preset["gap_rounds"]
    for gap_round in range(1, rounds + 1):
        lo = 0.6 + 0.2 * (gap_round - 1) / rounds
        hi = 0.6 + 0.2 * gap_round / rounds
        progress(
            f"Gap check {gap_round}/{rounds}: reviewing {sum(len(r['notes']) for r in results)} findings."
        )
        try:
            review = reply_json(
                judge(
                    f"gaps{gap_round}",
                    prompts.gaps(
                        question,
                        ctx.today,
                        notes_by_task(results),
                        preset["gap_workers"],
                    ),
                    (lo, lo),
                )
            )
        except (
            RuntimeError,
            RunnerError,
            ValueError,
        ) as e:  # an optional step; write from what's found
            progress(f"Gap check failed ({e}); writing from the findings so far.")
            break
        follow_ups = tasks_from(review.get("follow_ups"), preset["gap_workers"])
        if not follow_ups:
            progress("Gap check: the notes cover the question; no follow-ups.")
            break
        progress(
            f"Gap check: {clean(review.get('assessment'))} Following up on {len(follow_ups)} points."
        )
        results.extend(run_round(follow_ups, f"g{gap_round}-", (lo, hi)))

    notes = [n for r in results for n in r["notes"]]
    if not notes:
        if all(r["detail"]["stopped"] == "failed" for r in results):
            raise RuntimeError("Every worker failed; see the delegations' run log.")
        raise RuntimeError(
            "The workers found no usable, verifiable facts for this question."
        )

    findings = notes_by_source(notes, sources)
    ctx.meter(0.82)
    progress(
        f"Writing the report from {len(notes)} findings across {len({n['source_id'] for n in notes})} sources."
    )
    draft = None
    write = "ok"
    try:
        draft = judge(
            "write", prompts.write(question, ctx.today, findings), (0.82, 0.9)
        )
        if not draft.strip():
            raise RuntimeError("the reply was empty")
    except (RuntimeError, RunnerError) as e:  # fall back to the findings as the report
        draft = None
        write = f"failed: {e}"
        progress(f"Writing the report failed ({e}); using the findings as the report.")

    report = draft if draft is not None else notes_report(notes)
    edits = 0
    fact_check = "skipped"  # the findings are already quote-checked
    if draft is not None:
        ctx.meter(0.92)
        progress("Fact-checking the report against the notes.")
        fact_check = "ok"
        try:
            check = reply_json(
                judge("verify", prompts.verify(draft, findings), (0.92, 0.95))
            )
            report, edits = apply_edits(draft, check.get("edits"))
        except (
            RuntimeError,
            RunnerError,
            ValueError,
        ) as e:  # an optional step; keep the draft
            fact_check = f"failed: {e}"
            progress(f"Fact-check failed ({e}); keeping the draft.")

    markdown, used = finalize_citations(report, sources)
    return {
        "title": title,
        "question": question,
        "depth": preset["name"],
        "markdown": markdown,
        "sources": used,
        "summary": summary_bullets(markdown),
        "stats": {
            "engine": "agents",
            "seconds": round(time.monotonic() - started),
            "workers": len(results),
            "workers_detail": [r["detail"] for r in results],
            "findings": len(notes),
            "dropped_quotes": dropped,
            # AnythingLLM's web tools don't report these.
            "searches": None,
            "pages_read": None,
            "sources": len(used),
            "write": write,
            "fact_check": fact_check,
            "fact_check_edits": edits,
            "delegations": spent.delegations,
            "cost": round(spent.cost, 6),
            "tokens_by_model": spent.tokens,
        },
    }
