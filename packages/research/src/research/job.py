"""One research run from start to finish: research the question, save the report, write the
run log, and say what to tell the user.

research-runner runs these for the skill; `research-run` runs one by hand:
    research-run "Why is the sky blue?" --depth quick

Config (environment, from host.env and the unit; Settings.from_env):
  ANYTHINGLLM_STORAGE  storage directory (hostrpc.storage)
  ANYTHINGLLM_ENV      AnythingLLM's .env, for the model keys (default <storage>/.env)
  SEARXNG_URL          the SearXNG to search (default the host's; publicweb.pages)
  RESEARCH_LIVE_PORT   where the live cards listen (default 8450)
  PUBLIC_HOST          the machine's HTTPS name, for the live cards' URLs
  USER_TIMEZONE        the user's time zone, for the report's date (default Europe/Stockholm)
"""

import argparse
import os
import re
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import hostrpc
from llm import provider_for
from publicweb.pages import make_search, searxng_client, searxng_url
from runs.runlog import RunLog

from research import publish
from research.config import RESULTS_PER_SEARCH, SEARCH_GAP
from research.llm import LLM
from research.pipeline import Context, research
from research.web import make_reader, page_client

OFF = re.compile(r"^(no|off|none|false|0)$", re.IGNORECASE)
PAGES_PORT = 8445  # the pages site, where the live cards are routed
KEY_FINDINGS = 12  # summary bullets kept in the run log, for the chat's notice


def today(now: datetime | None = None) -> str:
    """Today's date in the user's time zone (USER_TIMEZONE)."""
    try:
        tz = ZoneInfo(os.environ.get("USER_TIMEZONE") or "Europe/Stockholm")
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("Europe/Stockholm")
    return (now or datetime.now(tz)).astimezone(tz).date().isoformat()


def pages_url() -> str:
    """The pages site's public URL, from PUBLIC_HOST; "" without it (and no live cards)."""
    host = os.environ.get("PUBLIC_HOST", "").strip()
    return f"https://{host}:{PAGES_PORT}/" if host else ""


@dataclass
class Settings:
    storage: Path
    searxng_url: str
    env_file: str  # AnythingLLM's .env, for the model keys
    runlogs: Path  # the run log and live runs' markers, host-only
    pages_url: str = (
        ""  # the pages site's public URL, for the live cards (research.live)
    )
    live_port: int = 8450  # where research.live listens

    @classmethod
    def from_env(cls) -> "Settings":
        get = os.environ.get
        storage = hostrpc.storage()
        return cls(
            storage=storage,
            searxng_url=searxng_url(),
            env_file=get("ANYTHINGLLM_ENV", str(storage / ".env")),
            runlogs=hostrpc.data_dir() / "research" / "runs",
            pages_url=pages_url(),
            live_port=int(get("RESEARCH_LIVE_PORT", "8450")),
        )

    @property
    def reports_dir(self) -> Path:
        # The built-in filesystem tools work in anythingllm-fs; reports go in a folder there.
        return self.storage / "anythingllm-fs" / "research"


@dataclass
class Request:
    """What the skill sends: its question and setup args."""

    question: str
    depth: str | None = None
    planner: str = "glm-5.3"
    worker: str = "deepseek-flash"
    planner_fallback: str = "deepseek-flash"
    # The calling agent's own split of the question, and the report's title then.
    sub_questions: list | None = None
    title: str | None = None
    # Set by research-runner, not the skill: the run's id and its live progress card,
    # kept in the run log.
    run_id: str | None = None
    card: str = ""

    @classmethod
    def of(cls, question: str, **args) -> "Request":
        """A request from what was given; arguments that are None or "" take the defaults."""
        return cls(
            question.strip(), **{k: v for k, v in args.items() if v not in (None, "")}
        )

    @property
    def models(self) -> dict:
        return {"planner": self.planner, "worker": self.worker}

    @property
    def fallback(self) -> dict:
        # When the GLM plan's usage is spent, the planner's calls go to DeepSeek instead.
        fallback = str(self.planner_fallback or "").strip()
        return (
            {self.planner: fallback}
            if provider_for(self.planner) == "zai"
            and fallback
            and not OFF.match(fallback)
            else {}
        )


def run(
    req: Request,
    settings: Settings,
    progress: Callable[[str], None],
    chat_closed: Callable[[], bool] = lambda: False,
    meter: Callable[[float], None] = lambda _: None,
    llm: LLM | None = None,
    search=None,
    read=None,
) -> dict:
    """Run it; never raises. Returns {status, reply, sources, title, file, error}: `reply`
    is what the caller is told (with the whole report), `sources` the cited pages, `title`
    and `file` the saved report's, and `error` why it failed. `meter` hears how far along
    the run is, from 0 to 1."""
    runlog = RunLog(settings.runlogs)
    record = {"question": req.question, "depth": req.depth, "models": req.models}
    if req.run_id:
        record.update(run_id=req.run_id, card=req.card)
    runlog.start(record)
    outcome = {**record, "status": "failed"}

    def note(message: str) -> None:
        runlog.event(message)
        progress(message)

    sources: list[dict] = []
    try:
        with ExitStack() as clients:
            reply = _run(
                req,
                settings,
                note,
                meter,
                outcome,
                sources,
                clients,
                llm,
                search,
                read,
            )
    except Exception as e:  # noqa: BLE001 - run() never raises; any failure goes in the reply and the log
        outcome["error"] = str(e) or type(e).__name__
        reply = f"The deep research run failed: {outcome['error']}. Tell the user what went wrong; don't retry on your own."
    if chat_closed():
        outcome["chat_closed"] = True
    try:
        runlog.write(outcome)
    except OSError as e:
        print(f"couldn't write the run log: {e}", file=sys.stderr)
    return {
        "status": outcome["status"],
        "reply": reply,
        "sources": sources,
        "title": outcome.get("title"),
        "file": outcome.get("file"),
        "error": outcome.get("error"),
    }


def _run(
    req,
    settings,
    progress,
    meter,
    outcome,
    sources,
    clients: ExitStack,
    llm,
    search,
    read,
) -> str:
    """The run itself; what it opens goes on `clients`, closed when the run ends."""
    date = today()
    if llm is None:
        llm = LLM.for_models(
            [req.planner, req.worker],
            settings.env_file,
            fallback=req.fallback,
            on_fallback=lambda frm, to, e: progress(
                f"{frm} is out of quota ({e}); using {to} instead"
            ),
        )
        clients.callback(llm.close)
    ctx = Context(
        llm=llm,
        search=search
        or make_search(
            settings.searxng_url,
            clients.enter_context(searxng_client()),
            SEARCH_GAP,
            RESULTS_PER_SEARCH,
        ),
        read=read or make_reader(clients.enter_context(page_client())),
        progress=progress,
        models=req.models,
        today=date,
        meter=meter,
    )
    report = research(req.question, req.depth, ctx, req.sub_questions, req.title)
    sources.extend({"url": s["url"], "title": s["title"]} for s in report["sources"])
    stats = report["stats"]
    bullets = "\n".join(f"- {re.sub(r'\s*\[\d+\]', '', b)}" for b in report["summary"])
    basis = (
        f"Based on {stats['sources']} sources and {stats['findings']} quote-checked findings; "
        f"took {max(1, round(stats['seconds'] / 60))} min ({report['depth']})."
        + (
            ""
            if stats["write"] == "ok"
            else f" Writing the report {stats['write']}, so it lists the findings as collected."
        )
    )

    meter(0.95)
    text = publish.report_file(
        report["title"], date, report["question"], report["markdown"]
    )
    try:
        file = publish.save_report(settings.reports_dir, report["title"], text)
    except OSError as e:
        raise RuntimeError(f"couldn't save the report: {e}") from None
    saved = os.path.relpath(file, settings.storage / "anythingllm-fs")
    progress(f"saved the report as {saved}")
    outcome.update(
        status="ok",
        depth=report["depth"],
        title=report["title"],
        stats=stats,
        file=str(file),
        summary=[re.sub(r"\s*\[\d+\]", "", b) for b in report["summary"]][
            :KEY_FINDINGS
        ],
    )
    return "\n\n".join(
        filter(
            None,
            [
                f'Research report done: "{report["title"]}"',
                (
                    f"Saved as {saved} in the agent's files. A run from a workspace's "
                    "chat is added to that workspace's documents too."
                ),
                f"Key findings:\n{bullets}" if bullets else "",
                basis,
                (
                    "Give the user the key findings in your own words; the whole report "
                    "follows. The research is finished; don't search again."
                ),
                "<report>\n"
                + re.sub(r"(?i)</\s*report", r"<\\/report", text)
                + "</report>",
            ],
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="research-run", description="Run one deep-research run in this process."
    )
    parser.add_argument("question")
    parser.add_argument("--depth", choices=["quick", "standard", "thorough"])
    parser.add_argument("--planner", help=f"default {Request.planner}")
    parser.add_argument("--worker", help=f"default {Request.worker}")
    args = parser.parse_args()
    req = Request.of(
        args.question,
        depth=args.depth,
        planner=args.planner,
        worker=args.worker,
    )
    result = run(
        req, Settings.from_env(), lambda m: print(m, file=sys.stderr, flush=True)
    )
    print(result["reply"])
    sys.exit(0 if result["status"] == "ok" else 1)
