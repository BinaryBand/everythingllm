import json
import random
import re
import threading

import pytest
from research.llm import LLM
from research.pipeline import Context, research

PAGES = {
    "https://a.example/": "Alpha Corp reported revenue of 12 million euros in 2025. Its CEO is Jane Doe.",
    "https://b.example/": "Beta Ltd was founded in 1999 in Uppsala and employs 300 people.",
}


def found(pattern: str, text: str) -> re.Match[str]:
    """The match the scripted model relies on being in its prompt."""
    m = re.search(pattern, text)
    assert m, f"{pattern!r} not in the prompt"
    return m


class Scripted:
    """A scripted model: answers by which stage's system prompt it was sent."""

    def __init__(
        self,
        on_create=lambda model, messages, think: None,
        always_search=False,
        override=None,
    ):
        self.on_create = on_create
        self.always_search = always_search
        self.override = override  # (system prompt, user prompt) -> reply text, or None to answer as usual
        self.lock = threading.Lock()

    def create(self, model, messages, max_tokens, think=True):
        with self.lock:
            self.on_create(model, messages, think)
        sys, usr = (
            messages[0]["content"],
            messages[1]["content"] if len(messages) > 1 else "",
        )
        if self.override and (text := self.override(sys, usr)) is not None:
            return text, {}
        if "planning a web research project" in sys:
            out = {
                "title": "Alpha and Beta",
                "sub_questions": [
                    {"goal": "Alpha finances", "queries": ["alpha revenue"]},
                    {"goal": "Beta background", "queries": ["beta founded"]},
                    {"goal": "Gamma", "queries": []},
                ],
            }
        elif "one part of a larger research question" in sys:
            goal = found(r"Your goal: (.*)", usr).group(1)
            if self.always_search:
                return json.dumps(
                    {"action": "search", "query": f"{goal} {random.random()}"}
                ), {}
            url = (
                "https://a.example/"
                if goal == "Alpha finances"
                else "https://b.example/"
            )
            done = f"Pages read: {url}" in usr or goal == "Gamma"
            out = (
                {"action": "done", "summary": "ok"}
                if done
                else {"action": "read", "url": url}
            )
        elif "extract facts from a web page" in sys:
            out = (
                {
                    "page_title": "Alpha news",
                    "findings": [
                        {
                            "claim": "Alpha's 2025 revenue was EUR 12M",
                            "quote": "Alpha Corp reported revenue of 12 million euros in 2025.",
                        },
                        {
                            "claim": "Alpha made a loss",
                            "quote": "Alpha Corp reported a loss of 3 million euros.",
                        },
                    ],
                }
                if "a.example" in usr
                else {
                    "page_title": "Beta about",
                    "findings": [
                        {
                            "claim": "Beta was founded in 1999 in Uppsala",
                            "quote": "Beta Ltd was founded in 1999 in Uppsala",
                        },
                    ],
                }
            )
        elif "fact-checking a research report" in sys:
            out = {
                "edits": [
                    {
                        "find": found(
                            r"Alpha is the largest company in Europe \[\d+\]\.", usr
                        ).group(0),
                        "replace": "",
                    }
                ]
            }
        elif "writing a research report" in sys:
            alpha = found(r"\[(\d+)\] Alpha news", usr).group(1)
            beta = found(r"\[(\d+)\] Beta about", usr).group(1)
            return (
                f"## Summary\n\n- Alpha earned EUR 12M in 2025 [{alpha}].\n- Beta dates from 1999 [{beta}].\n\n"
                f"## Details\n\nAlpha is the largest company in Europe [{alpha}]. Beta is in Uppsala [{beta}, 42].\n"
            ), {}
        elif "reviewing your team's notes" in sys:
            out = {"assessment": "Covered.", "follow_ups": []}
        else:
            raise AssertionError(f"unexpected prompt: {sys[:60]}")
        return json.dumps(out), {}


def context(client=None):
    lines = []
    ctx = Context(
        llm=LLM(client or Scripted()),
        search=lambda q: [
            {"title": "Alpha news", "url": "https://a.example/", "snippet": q},
            {"title": "Beta about", "url": "https://b.example/", "snippet": q},
        ],
        read=PAGES.get,
        progress=lines.append,
        models={"planner": "pro", "worker": "flash"},
        today="2026-10-03",
    )
    return ctx, lines


def test_a_quick_run_plans_researches_writes_fact_checks_and_cites():
    ctx, lines = context()
    report = research("Tell me about Alpha and Beta", "quick", ctx)
    assert report["title"] == "Alpha and Beta"
    assert report["depth"] == "quick"
    # The made-up quote was dropped; the two real ones were kept.
    stats = report["stats"]
    assert stats["findings"] == 2 and stats["dropped_quotes"] == 1
    # The fact-check removed the unsupported sentence; [42] isn't a source.
    assert stats["fact_check_edits"] == 1 and stats["fact_check"] == "ok"
    assert sorted(
        (w["goal"], w["findings"], w["reads"], w["stopped"])
        for w in stats["workers_detail"]
    ) == [
        ("Alpha finances", 1, 1, "done"),
        ("Beta background", 1, 1, "done"),
        ("Gamma", 0, 0, "done"),
    ]
    assert "largest company" not in report["markdown"]
    assert re.search(r"Beta is in Uppsala \[\d\]\.", report["markdown"])
    ids = {s["url"]: s["id"] for s in report["sources"]}
    assert report["summary"] == [
        f"Alpha earned EUR 12M in 2025 [{ids['https://a.example/']}].",
        f"Beta dates from 1999 [{ids['https://b.example/']}].",
    ]
    assert sorted(ids) == ["https://a.example/", "https://b.example/"]
    assert re.search(
        r"## Sources\n\n1\. \[(Alpha news|Beta about)\]", report["markdown"]
    )
    assert any(line.startswith("Planning quick research") for line in lines)
    assert report["stats"]["plan"] == "planner"
    assert any(
        re.search(r"read a\.example: 1 findings kept, 1 dropped", line)
        for line in lines
    )


def test_the_callers_sub_questions_take_the_planners_place():
    systems = []
    ctx, lines = context(
        Scripted(
            on_create=lambda m, messages, t: systems.append(messages[0]["content"])
        )
    )
    report = research(
        "Tell me about Alpha and Beta",
        "quick",
        ctx,
        sub_questions=[
            {"goal": "Alpha finances", "queries": ["alpha revenue"]},
            "Beta background",
            "  ",
            "Gamma",
            "Delta",  # over quick's 3 workers
        ],
        title="  Alpha, Beta ",
    )
    assert not any("planning a web research project" in s for s in systems)
    assert report["title"] == "Alpha, Beta" and report["stats"]["plan"] == "caller"
    assert [w["goal"] for w in report["stats"]["workers_detail"]] == [
        "Alpha finances",
        "Beta background",
        "Gamma",
    ]
    assert report["stats"]["findings"] == 2
    assert (
        "Plan (from the caller): 1) Alpha finances 2) Beta background 3) Gamma" in lines
    )
    untitled = research(
        "Tell me about Alpha", "quick", context()[0], ["Alpha finances"]
    )
    assert untitled["title"] == "Tell me about Alpha"
    with pytest.raises(RuntimeError, match="sub_questions had no goals"):
        research("q", "quick", context()[0], [" ", {"goal": ""}])


def test_workers_use_flash_without_thinking_the_planner_uses_pro():
    seen = []
    ctx, _ = context(
        Scripted(on_create=lambda model, messages, think: seen.append((model, think)))
    )
    research("q", "quick", ctx)
    assert any(m == "flash" and not think for m, think in seen)
    assert all((m == "flash") == (not think) for m, think in seen)
    assert sum(1 for m, _ in seen if m == "pro") >= 3


def test_a_run_with_no_verifiable_findings_fails_instead_of_writing():
    ctx, _ = context()
    ctx.read = lambda url: None
    with pytest.raises(RuntimeError, match="no usable, verifiable facts"):
        research("q", "quick", ctx)


def test_when_every_search_fails_workers_stop_early_and_the_error_says_why():
    ctx, _ = context()
    ctx.read = lambda url: (
        None
    )  # the scripted workers would otherwise read pages without searching
    searches = []

    def search(q):
        searches.append(q)
        raise RuntimeError(
            "no results; engines unavailable: google cse (Suspended: too many requests)"
        )

    ctx.search = search
    with pytest.raises(
        RuntimeError,
        match=r"Web search isn't working: all \d+ searches failed .*google cse",
    ):
        research("q", "quick", ctx)
    assert len(searches) <= 6, f"kept searching after failures ({len(searches)})"


def test_a_worker_stops_once_its_notes_are_full():
    many = [f"Sentence number {i} about Alpha Corp and its revenue." for i in range(8)]
    extractions = [0]

    def override(sys, usr):
        if "extract facts from a web page" in sys:
            extractions[0] += 1
            return json.dumps({"findings": [{"claim": q, "quote": q} for q in many]})
        if "one part of a larger research question" in sys:
            return json.dumps(
                {"action": "read", "url": f"https://p.example/{random.random()}"}
            )
        return None

    ctx, lines = context(Scripted(override=override))
    ctx.read = lambda url: " ".join(many) + f" {url}"
    research("q", "quick", ctx)
    # 3 workers x 30-note cap at 8 findings a page: 4 reads each fill the notes.
    assert extractions[0] == 12
    assert any("8 findings kept" in line for line in lines)
    assert any("6 findings kept, 2 over the 30-note cap" in line for line in lines)


def test_a_run_never_goes_over_its_depths_search_budget():
    ctx, _ = context(Scripted(always_search=True))
    ctx.read = lambda url: None
    searches = []
    search = ctx.search
    ctx.search = lambda q: (searches.append(q), search(q))[1]
    with pytest.raises(RuntimeError, match="no usable, verifiable facts"):
        research("q", "quick", ctx)
    assert len(searches) == 15


def failing_on(marker):
    def override(sys, usr):
        if marker in sys:
            raise RuntimeError(f"{marker}: 503")

    return Scripted(override=override)


def test_a_failed_gap_check_goes_straight_to_writing():
    ctx, lines = context(failing_on("reviewing your team's notes"))
    report = research("q", "standard", ctx)
    assert any(
        re.fullmatch(
            r"Gap check failed \(.*503\); writing from the findings so far\.", line
        )
        for line in lines
    )
    assert report["stats"]["write"] == "ok" and report["stats"]["fact_check"] == "ok"
    assert "Alpha earned EUR 12M" in report["markdown"]


def test_when_writing_fails_the_findings_become_the_report():
    ctx, lines = context(failing_on("writing a research report"))
    report = research("q", "quick", ctx)
    assert re.match(r"failed: .*503", report["stats"]["write"])
    assert report["stats"]["fact_check"] == "skipped"
    assert any(line.startswith("Writing the report failed") for line in lines)
    md = report["markdown"]
    assert md.startswith("_Writing the report failed")
    assert re.search(
        r"## Alpha finances\n\n- Alpha's 2025 revenue was EUR 12M \[\d\]", md
    )
    assert re.search(
        r"## Beta background\n\n- Beta was founded in 1999 in Uppsala \[\d\]", md
    )
    assert re.search(r"## Sources\n\n1\. \[(Alpha news|Beta about)\]", md)
    assert len(report["sources"]) == 2
