"""One research run from start to finish: research the question, save and publish the
report, add it to the workspace, write the run log, and say what to tell the user.

research-runner runs these for the skill; `research-run` runs one by hand:
    research-run "Why is the sky blue?" --depth quick
"""

import argparse
import os
import re
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import hostrpc
from llm import provider_for
from publicweb.pages import SEARXNG, make_search, searxng_client
from runs.runlog import RunLog
from sites import cards
from sites.build import Builder
from sites.store import SiteStore, pages_url
from sites.store import today as sites_today

from research import publish
from research.config import RESULTS_PER_SEARCH, SEARCH_GAP
from research.llm import LLM
from research.pipeline import Context, research
from research.web import make_reader, page_client

OFF = re.compile(r"^(no|off|none|false|0)$", re.IGNORECASE)


@dataclass
class Settings:
    storage: Path
    searxng_url: str
    api: str  # AnythingLLM's API, for embedding
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
            searxng_url=SEARXNG,
            api="http://127.0.0.1:3001/api",
            env_file=get("ANYTHINGLLM_ENV", str(storage / ".env")),
            runlogs=hostrpc.data_dir() / "research" / "runs",
            pages_url=pages_url(Builder.from_env().source),
            live_port=int(get("RESEARCH_LIVE_PORT", "8450")),
        )

    @property
    def reports_dir(self) -> Path:
        # The built-in filesystem tools work in anythingllm-fs; reports go in a folder there.
        return self.storage / "anythingllm-fs" / "research"

    @property
    def documents_dir(self) -> Path:
        return self.storage / "documents"


@dataclass
class Request:
    """What the skill sends: its question and setup args, and the workspace it ran in."""

    question: str
    depth: str | None = None
    planner: str = "glm-5.3"
    worker: str = "deepseek-flash"
    planner_fallback: str = "deepseek-flash"
    site: str = "research"
    embed: bool = True
    workspace: str | None = None  # slug
    workspace_name: str | None = None
    # Set by research-runner, not the skill: the run's id and its live progress card,
    # kept in the run log so the audit can hand the card out again.
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
    builder: Builder | None = None,
    llm: LLM | None = None,
    search=None,
    read=None,
) -> dict:
    """Run it; never raises. Returns {status, reply, sources, url, title, error}: `reply`
    is what the agent is told, `sources` the cited pages, for the chat's citations, `url`
    and `title` the published report's, when there is one, and `error` why it failed. `meter` hears how far along the
    run is, from 0 to 1."""
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
                builder,
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
        "url": outcome.get("url"),
        "title": outcome.get("title"),
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
    builder,
    llm,
    search,
    read,
) -> str:
    """The run itself; what it opens goes on `clients`, closed when the run ends."""
    builder = builder or Builder.from_env()
    # Fail before spending tokens if there's nowhere to publish.
    if not (builder.source / req.site / "zola.toml").is_file():
        raise RuntimeError(f"no Zola site named '{req.site}' in {builder.source}.")
    today = sites_today()
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
        today=today,
        meter=meter,
    )
    report = research(req.question, req.depth, ctx)
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

    def in_files(file: str) -> str:
        return os.path.relpath(file, settings.storage / "anythingllm-fs")

    # The file is saved before publishing: the site doesn't keep an entry it couldn't
    # build, and the research shouldn't be lost with it.
    def file_text(url: str | None) -> str:
        return publish.report_file(
            report["title"], today, report["question"], url, report["markdown"]
        )

    meter(0.95)
    store = SiteStore(builder.source, builder.content, build=builder.build, agent=True)
    copies = publish.save_then_publish(
        settings.reports_dir,
        report["title"],
        file_text,
        lambda: store.write(
            req.site,
            "reports",
            None,
            report["title"],
            today,
            {
                "question": report["question"],
                "depth": report["depth"],
                "models": req.models,
                "stats": stats,
            },
            report["markdown"],
        ),
    )
    build = copies.pop("build", None)
    publish_error = copies.pop("publish_error", None)
    if not build and "file" not in copies:
        raise RuntimeError(
            f"couldn't publish the report ({publish_error}) or save it ({copies.get('file_error')})"
        )
    outcome.update(
        status="ok", depth=report["depth"], title=report["title"], stats=stats
    )
    if not build:
        progress(
            f"couldn't publish the report ({publish_error}); saved it as {in_files(copies['file'])}"
        )
        outcome.update(published=False, build_error=publish_error, file=copies["file"])
        return "\n\n".join(
            filter(
                None,
                [
                    f'Research report "{report["title"]}" couldn\'t be published to the research site: {publish_error}',
                    f"The report is saved as {in_files(copies['file'])} in the agent's files.",
                    f"Key findings:\n{bullets}" if bullets else "",
                    basis,
                    (
                        "Tell the user where the report is saved and why it wasn't published, and give the key findings in "
                        "your own words. The research is finished; don't search again or retry publishing."
                    ),
                ],
            )
        )
    url = build.url
    progress(f"published {url}")
    card = cards.entry_card(
        builder.output,
        store,
        req.site,
        build,
        {"question": report["question"]},
        report["markdown"],
    )

    # A copy in the workspace. The report is already saved, so this only warns.
    if req.embed and req.workspace:
        meter(0.98)
        progress(
            f'adding the report to workspace "{req.workspace_name or req.workspace}"'
        )
        try:
            copies["document"] = publish.embed_report(
                req.workspace,
                settings.documents_dir,
                "deep-research",
                build.slug,
                report["title"],
                url,
                file_text(url),
                settings.api,
                login=lambda fresh: hostrpc.anythingllm_headers(
                    settings.api, settings.env_file, fresh=fresh
                ),
            )
        except Exception as e:  # noqa: BLE001 - the report is already published; this copy only warns
            copies["document_error"] = str(e)
            progress(f"couldn't add the report to the workspace: {e}")
    outcome.update(url=url, published=True, build_error=None, **copies)
    return "\n\n".join(
        filter(
            None,
            [
                f'Research report published: "{report["title"]}"',
                f"Link: {url}",
                f"Card: {card}" if card else "",
                f"Saved as {in_files(copies['file'])} in the agent's files."
                if "file" in copies
                else f"Couldn't save a copy to the agent's files: {copies.get('file_error')}",
                "Added to this workspace's documents, so later chats can search it."
                if "document" in copies
                else f"Couldn't add it to this workspace's documents: {copies['document_error']}"
                if "document_error" in copies
                else "",
                f"Key findings:\n{bullets}" if bullets else "",
                basis,
                "Give the user the link and the key findings in your own words. The research is finished; don't search again.",
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
    parser.add_argument("--workspace", help="slug of a workspace to add the report to")
    args = parser.parse_args()
    req = Request.of(
        args.question,
        depth=args.depth,
        planner=args.planner,
        worker=args.worker,
        workspace=args.workspace,
    )
    result = run(
        req, Settings.from_env(), lambda m: print(m, file=sys.stderr, flush=True)
    )
    print(result["reply"])
    sys.exit(0 if result["status"] == "ok" else 1)
