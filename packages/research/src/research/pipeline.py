"""The research run: plan, parallel workers, gap rounds, write, fact-check."""

import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from publicweb import host_name as host
from publicweb.pages import Search

from research import config as cfg
from research import prompts
from research.llm import LLM
from research.sources import (
    SourceList,
    finalize_citations,
    quote_in_text,
    summary_bullets,
)
from research.web import Read


def clean(s) -> str:
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip()


def tasks_from(items, max: int) -> list[dict]:
    if not isinstance(items, list):
        return []
    tasks = []
    for t in items:
        if not isinstance(t, dict) or not clean(t.get("goal")):
            continue
        queries = t.get("queries") if isinstance(t.get("queries"), list) else []
        tasks.append(
            {
                "goal": clean(t["goal"]),
                "queries": [q for q in map(clean, queries) if q][:3],
            }
        )
    return tasks[:max]


@dataclass
class Context:
    llm: LLM
    search: Search
    read: Read
    progress: Callable[[str], None]
    models: dict  # {"planner": ..., "worker": ...}
    today: str  # YYYY-MM-DD
    # How far along the run is, from 0 to 1, for research-runner's live progress image.
    meter: Callable[[float], None] = lambda _: None


@dataclass
class Counts:
    search_budget: int
    searches: int = 0
    failed_searches: int = 0
    reads: int = 0
    dropped: int = 0
    last_search_error: str = ""
    # Workers run in threads; this guards the counts and the source list.
    lock: threading.Lock = field(default_factory=threading.Lock)

    def take_search(self) -> bool:
        """Count a search before it runs, so workers queued behind one another can't
        overshoot the budget together. False when the budget is used up."""
        with self.lock:
            if self.searches >= self.search_budget:
                return False
            self.searches += 1
            return True

    def left(self) -> int:
        return max(0, self.search_budget - self.searches)


def research(question: str, depth: str | None, ctx: Context) -> dict:
    """Research a question and return a finished, cited Markdown report."""
    llm, models, progress = ctx.llm, ctx.models, ctx.progress
    question = clean(question)
    if not question:
        raise ValueError("No research question was given.")
    preset = cfg.depth_preset(depth)
    started = time.monotonic()
    sources = SourceList()
    counts = Counts(search_budget=preset["searches"])

    ctx.meter(0.03)
    progress(
        f"Planning {preset['name']} research: {preset['workers']} parallel workers, up to {preset['steps']} steps each."
    )
    plan = llm.json(
        models["planner"], prompts.plan(question, ctx.today, preset["workers"])
    )
    title = clean(plan.get("title"))[:120] or question[:120]
    tasks = tasks_from(plan.get("sub_questions"), preset["workers"])
    if not tasks:
        raise RuntimeError("The planner returned no sub-questions.")
    progress("Plan: " + " ".join(f"{i + 1}) {t['goal']}" for i, t in enumerate(tasks)))

    def run_round(batch: list[dict], label: str) -> list[dict]:
        with ThreadPoolExecutor(
            max_workers=len(batch), thread_name_prefix="worker"
        ) as pool:
            futures = [
                pool.submit(
                    worker,
                    question,
                    task,
                    f"{label}{i + 1}/{len(batch)}",
                    preset["steps"],
                    ctx,
                    sources,
                    counts,
                )
                for i, task in enumerate(batch)
            ]
        done = []
        for i, f in enumerate(futures):
            try:
                done.append(f.result())
            except Exception as e:  # noqa: BLE001 - one worker failing doesn't sink the run
                progress(f"Worker {label}{i + 1} failed: {e}")
        return done

    results = run_round(tasks, "")
    for gap_round in range(1, preset["gap_rounds"] + 1):
        if not counts.left():
            progress(
                f"Search budget ({counts.search_budget}) used up; skipping further gap checks."
            )
            break
        progress(
            f"Gap check {gap_round}/{preset['gap_rounds']}: reviewing {sum(len(r['notes']) for r in results)} findings."
        )
        try:
            review = llm.json(
                models["planner"],
                prompts.gaps(
                    question, ctx.today, notes_by_task(results), preset["gap_workers"]
                ),
            )
        except Exception as e:  # noqa: BLE001 - an optional step; write from what's found
            progress(f"Gap check failed ({e}); writing from the findings so far.")
            break
        follow_ups = tasks_from(review.get("follow_ups"), preset["gap_workers"])
        if not follow_ups:
            progress("Gap check: the notes cover the question; no follow-ups.")
            break
        progress(
            f"Gap check: {clean(review.get('assessment'))} Following up on {len(follow_ups)} points."
        )
        results.extend(run_round(follow_ups, f"G{gap_round}."))

    notes = [n for r in results for n in r["notes"]]
    if not notes:
        if counts.searches and counts.failed_searches == counts.searches:
            raise RuntimeError(
                f"Web search isn't working: all {counts.searches} searches failed (last: {counts.last_search_error}). "
                "SearXNG's engines are probably rate-limited; try again later."
            )
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
        draft = llm.chat(
            models["planner"],
            prompts.write(question, ctx.today, findings),
            max_tokens=cfg.MAX_TOKENS["write"],
        )
    except Exception as e:  # noqa: BLE001 - fall back to the findings as the report
        # Don't lose the research: the findings themselves become the report.
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
            check = llm.json(
                models["planner"],
                prompts.verify(draft, findings),
                max_tokens=cfg.MAX_TOKENS["verify"],
            )
            report, edits = apply_edits(draft, check.get("edits"))
        except Exception as e:  # noqa: BLE001 - an optional step; keep the draft
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
            "engine": "pipeline",
            "seconds": round(time.monotonic() - started),
            "workers": len(results),
            "workers_detail": [r["detail"] for r in results],
            "findings": len(notes),
            "dropped_quotes": counts.dropped,
            "searches": counts.searches,
            "failed_searches": counts.failed_searches,
            "pages_read": counts.reads,
            "sources": len(used),
            "write": write,
            "fact_check": fact_check,
            "fact_check_edits": edits,
            "llm_calls": llm.usage["calls"],
            "fallbacks": dict(llm.switched),
            "tokens": {
                k: llm.usage[k] for k in ("prompt", "cached", "completion", "reasoning")
            },
            "tokens_by_model": {m: dict(t) for m, t in llm.by_model.items()},
        },
    }


def worker(
    question: str,
    task: dict,
    tag: str,
    budget: int,
    ctx: Context,
    sources: SourceList,
    counts: Counts,
) -> dict:
    """One worker: search and read toward a single goal, keeping only quote-checked findings."""
    llm, models, progress = ctx.llm, ctx.models, ctx.progress
    notes: list[dict] = []
    queries: list[str] = []
    read_urls: list[str] = []
    titles: dict[str, str] = {}
    results: list[dict] = []
    failed_in_a_row = failed_searches = 0

    def notes_full() -> bool:
        return len(notes) >= cfg.NOTES_PER_WORKER

    def do_search(query: str) -> bool:
        nonlocal results, failed_in_a_row, failed_searches
        if not counts.take_search():
            return False
        queries.append(query)
        # The searches are most of a run's time, so they fill most of the bar.
        ctx.meter(0.05 + 0.75 * counts.searches / max(1, counts.search_budget))
        progress(f'[{tag}] searching "{query}"')
        try:
            results = ctx.search(query)
            failed_in_a_row = 0
        except Exception as e:  # noqa: BLE001 - a failed search is counted and reported, not fatal
            failed_in_a_row += 1
            failed_searches += 1
            with counts.lock:
                counts.failed_searches += 1
                counts.last_search_error = str(e)
            progress(f"[{tag}] search failed: {e}")
            results = []
        for r in results:
            titles.setdefault(r["url"], r["title"])
        return True

    def do_read(url: str) -> None:
        read_urls.append(url)
        with counts.lock:
            counts.reads += 1
        text = ctx.read(url)
        if not text:
            progress(f"[{tag}] couldn't read {host(url)}")
            return
        try:
            out = llm.json(
                models["worker"],
                prompts.extract(
                    question,
                    task["goal"],
                    url,
                    titles.get(url, url),
                    text,
                    cfg.FINDINGS_PER_PAGE,
                ),
                max_tokens=cfg.MAX_TOKENS["worker"],
                think=False,
            )
        except Exception as e:  # noqa: BLE001 - one page failing doesn't stop the worker
            progress(f"[{tag}] couldn't extract from {host(url)}: {e}")
            return
        found = out.get("findings")
        if not isinstance(found, list):
            found = []
        kept = dropped = over_cap = 0
        for f in found[: cfg.FINDINGS_PER_PAGE]:
            f = f if isinstance(f, dict) else {}
            claim, quote = clean(f.get("claim")), clean(f.get("quote"))
            if not claim or not quote_in_text(quote, text):
                dropped += 1
                continue
            if notes_full():
                over_cap += 1
                continue
            with counts.lock:
                source = sources.add(
                    url, clean(out.get("page_title")) or titles.get(url)
                )
            notes.append(
                {
                    "claim": claim,
                    "quote": quote,
                    "source_id": source["id"],
                    "goal": task["goal"],
                }
            )
            kept += 1
        with counts.lock:
            counts.dropped += dropped
        extra = "".join(
            f", {e}"
            for e in (
                dropped and f"{dropped} dropped (quote not on page)",
                over_cap and f"{over_cap} over the {cfg.NOTES_PER_WORKER}-note cap",
            )
            if e
        )
        progress(f"[{tag}] read {host(url)}: {kept} findings kept{extra}")

    if task["queries"]:
        do_search(task["queries"][0])

    wasted = 0
    summary = ""

    # Why the worker must stop now, or None to go on. Full notes: reading more would only
    # pay for extractions that get thrown away. Two failed searches in a row: search is
    # down, and more would only prolong a rate limit.
    def stop_reason() -> str | None:
        if notes_full():
            return "notes-full"
        if failed_in_a_row >= 2:
            return "search-down"
        return "wasted" if wasted >= 3 else None

    stopped = "budget"
    for step in range(budget):
        if reason := stop_reason():
            stopped = reason
            break
        try:
            action = llm.json(
                models["worker"],
                prompts.step(
                    question,
                    task["goal"],
                    ctx.today,
                    budget - step,
                    counts.left(),
                    queries,
                    results,
                    notes,
                    read_urls,
                ),
                max_tokens=cfg.MAX_TOKENS["worker"],
                think=False,
            )
        except Exception:  # noqa: BLE001 - a bad step counts as wasted; the worker goes on
            wasted += 1
            continue
        # deepseek-flash sometimes names the key "type".
        kind = clean(action.get("action") or action.get("type")).lower()
        if kind == "done":
            summary = clean(action.get("summary"))
            stopped = "done"
            break
        if kind == "search":
            query = clean(action.get("query"))
            if not query or query in queries or not do_search(query):
                wasted += 1
        elif kind == "read":
            url = clean(action.get("url"))
            if not re.match(r"^https?://", url) or url in read_urls:
                wasted += 1
            else:
                do_read(url)
        else:
            wasted += 1
    if stopped == "budget":
        stopped = stop_reason() or "budget"  # the last step may have tripped one
    progress(f'[{tag}] done ({stopped}): {len(notes)} findings for "{task["goal"]}"')
    return {
        "goal": task["goal"],
        "notes": notes,
        "summary": summary,
        # For the run log and the audit.
        "detail": {
            "goal": task["goal"],
            "findings": len(notes),
            "searches": len(queries),
            "reads": len(read_urls),
            "failed_searches": failed_searches,
            "stopped": stopped,
        },
    }


def notes_by_task(results: list[dict]) -> str:
    sections = []
    for r in results:
        lines = (
            "\n".join(f"- {n['claim']} [{n['source_id']}]" for n in r["notes"])
            or "- (nothing found)"
        )
        summary = f"Worker summary: {r['summary']}\n" if r["summary"] else ""
        sections.append(f"### {r['goal']}\n{summary}{lines}")
    return "\n\n".join(sections)


def notes_by_source(notes: list[dict], sources: SourceList) -> str:
    groups: dict[int, list[dict]] = {}
    for n in notes:
        groups.setdefault(n["source_id"], []).append(n)
    blocks = []
    for id in sorted(groups):
        s = sources.get(id)
        assert s is not None  # every note's source_id came from sources.add
        items = "\n".join(
            f'- {n["claim"]}\n  Quote: "{n["quote"]}"' for n in groups[id]
        )
        blocks.append(f"[{id}] {s['title']} ({s['url']})\n{items}")
    return "\n\n".join(blocks)


def notes_report(notes: list[dict]) -> str:
    """The report when writing it failed: the findings by sub-question, each citing its source."""
    groups: dict[str, list[str]] = {}
    for n in notes:
        groups.setdefault(n["goal"], []).append(f"- {n['claim']} [{n['source_id']}]")
    sections = [f"## {goal}\n\n" + "\n".join(lines) for goal, lines in groups.items()]
    return "\n\n".join(
        [
            "_Writing the report failed, so these are the research findings as collected._",
            *sections,
        ]
    )


def apply_edits(text: str, edits) -> tuple[str, int]:
    """Apply fact-check edits whose `find` text appears verbatim in the report."""
    applied = 0
    for e in edits if isinstance(edits, list) else []:
        find = (
            e.get("find").strip()
            if isinstance(e, dict) and isinstance(e.get("find"), str)
            else ""
        )
        if len(find) < 10 or find not in text:
            continue
        replace = e["replace"].strip() if isinstance(e.get("replace"), str) else ""
        text = text.replace(find, replace, 1)
        applied += 1
    return re.sub(r"\n{3,}", "\n\n", text), applied
