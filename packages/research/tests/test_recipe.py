import json
import re
from pathlib import Path

import pytest
from hostrpc import RunnerError
from publicweb.pages import Page
from research import recipe
from research.web import make_checker

PAGES = {
    "https://a.example/": Page(
        "Alpha news",
        "Alpha Corp reported revenue of 12 million euros in 2025. Its CEO is Jane Doe.",
        "Menu Shop About Alpha Corp reported revenue of 12 million euros in 2025. "
        "Its CEO is Jane Doe. Footer: Alpha Corp has 40 offices in Europe.",
    ),
    "https://b.example/": Page(
        "", "Beta Ltd was founded in 1999 in Uppsala and employs 300 people.", ""
    ),
}


def worker_reply(findings, summary="Found it."):
    body = json.dumps({"summary": summary, "findings": findings})
    return f"I searched and read two pages.\n\n```json\n{body}\n```"


class FakeRunner:
    """agents-runner's delegations, answered by task name and the goal in the instructions."""

    def __init__(self, write_fails=False, workers_fail=False):
        self.calls: list[tuple[str, list[dict], tuple[float, float]]] = []
        self.write_fails = write_fails
        self.workers_fail = workers_fail

    def reply(self, task: dict) -> dict:
        name, text = task["name"], task["instructions"]
        if name == "plan":
            plan = {
                "title": "Alpha and Beta",
                "sub_questions": [
                    {"goal": "Alpha finances", "queries": ["alpha revenue"]},
                    {"goal": "Beta background", "queries": []},
                    {"goal": "Gamma", "queries": []},
                ],
            }
            return {"text": f"Here's the plan:\n{json.dumps(plan)}"}
        if name.startswith("gaps"):
            return {
                "text": json.dumps(
                    {
                        "assessment": "Beta's size is thin.",
                        "follow_ups": [
                            {"goal": "Beta size", "queries": ["beta staff"]}
                        ],
                    }
                )
            }
        if name == "write":
            if self.write_fails:
                return {"status": "failed", "error": "GLM is out of quota"}
            alpha = re.search(r"\[(\d+)\] Alpha news", task["material"])
            beta = re.search(r"\[(\d+)\] b\.example", task["material"])
            assert alpha and beta
            a, b = alpha[1], beta[1]
            return {
                "text": f"## Summary\n\n- Alpha earned EUR 12M in 2025 [{a}].\n\n"
                f"## Details\n\nAlpha is the largest company in Europe [{a}]. "
                f"Beta is in Uppsala [{b}, 42].\n"
            }
        if name == "verify":
            sentence = re.search(
                r"Alpha is the largest company in Europe \[\d+\]\.", task["material"]
            )
            assert sentence
            return {
                "text": json.dumps({"edits": [{"find": sentence[0], "replace": ""}]})
            }
        # a worker
        if self.workers_fail or "Your part: Gamma" in text:
            return {"status": "failed", "error": "the agent gave no reply"}
        if "Your part: Alpha finances" in text:
            return {
                "text": worker_reply(
                    [
                        {
                            "claim": "Alpha's 2025 revenue was EUR 12M",
                            "quote": "Alpha Corp reported revenue of 12 million euros in 2025.",
                            "url": "https://a.example/",
                        },
                        {  # only outside the main text
                            "claim": "Alpha has 40 offices",
                            "quote": "Alpha Corp has 40 offices in Europe.",
                            "url": "https://a.example/",
                        },
                        {
                            "claim": "Alpha made a loss",
                            "quote": "Alpha Corp reported a loss of 3 million euros.",
                            "url": "https://a.example/",
                        },
                        {
                            "claim": "From nowhere",
                            "quote": "Alpha Corp reported revenue of 12 million euros in 2025.",
                            "url": "file:///etc/passwd",
                        },
                    ]
                )
            }
        if "Your part: Beta background" in text:
            return {"text": "I couldn't find much, sorry."}  # no JSON
        return {
            "text": worker_reply(
                [
                    {
                        "claim": "Beta was founded in 1999 in Uppsala",
                        "quote": "Beta Ltd was founded in 1999 in Uppsala",
                        "url": "https://b.example/",
                    }
                ]
            )
        }

    def __call__(self, goal, tasks, span):
        self.calls.append((goal, tasks, span))
        outcomes = [
            {
                "name": t["name"],
                "profile": t["profile"],
                "status": "ok",
                "seconds": 3.0,
                "model": f"model-{t['profile']}",
                "tokens": {"prompt": 100, "completion": 10},
                **self.reply(t),
            }
            for t in tasks
        ]
        return {
            "status": "ok",
            "tasks": outcomes,
            "cost": 0.001 * len(tasks),
            "tokens": {"model-x": {"prompt": 100 * len(tasks), "completion": 10}},
            "run_id": f"dg-{len(self.calls):08x}",
        }


def context(runner):
    lines = []
    return recipe.Context(
        delegate=runner,
        check=make_checker(fetch=PAGES.get),
        progress=lines.append,
        today="2026-10-06",
    ), lines


def test_a_standard_run_delegates_every_step_and_keeps_only_checked_quotes():
    runner = FakeRunner()
    ctx, lines = context(runner)
    report = recipe.research("Tell me about Alpha and Beta", "standard", ctx)
    assert report["title"] == "Alpha and Beta" and report["depth"] == "standard"
    stats = report["stats"]
    # Kept: the revenue (main text), the offices (only in the whole page) and Beta's
    # founding (from the gap round). Dropped: the made-up quote and the non-web URL.
    assert stats["findings"] == 3 and stats["dropped_quotes"] == 2
    assert stats["engine"] == "agents" and stats["searches"] is None
    assert [
        (w["goal"], w["findings"], w["dropped"], w["stopped"])
        for w in stats["workers_detail"]
    ] == [
        ("Alpha finances", 2, 2, "done"),
        ("Beta background", 0, 0, "no-json"),
        ("Gamma", 0, 0, "failed"),
        ("Beta size", 1, 0, "done"),
    ]
    assert stats["fact_check"] == "ok" and stats["fact_check_edits"] == 1
    assert stats["write"] == "ok"
    # plan, workers, gaps, follow-ups, write, verify
    assert stats["delegations"] == [f"dg-{n:08x}" for n in range(1, 7)]
    assert stats["cost"] == pytest.approx(0.001 * 8)  # 8 tasks
    assert stats["tokens_by_model"] == {"model-x": {"prompt": 800, "completion": 60}}
    assert "largest company" not in report["markdown"]
    assert re.search(r"Beta is in Uppsala \[\d\]\.", report["markdown"])  # no [42]
    assert {s["url"] for s in report["sources"]} == {
        "https://a.example/",
        "https://b.example/",
    }
    titles = {s["url"]: s["title"] for s in report["sources"]}
    assert titles["https://b.example/"] == "b.example"  # a page without a <title>

    # The planner's tasks are plain chats given their material; the workers' use tools.
    by_name = {t["name"]: t for _, tasks, _ in runner.calls for t in tasks}
    assert by_name["write"]["tools"] is False and by_name["plan"]["tools"] is False
    assert "Notes by source" in by_name["write"]["material"]
    assert "Question: Tell me about Alpha and Beta" in by_name["gaps1"]["material"]
    assert "tools" not in by_name["w1"] and by_name["w1"]["profile"] == "worker"
    assert 'Searches to start with: "alpha revenue"' in by_name["w1"]["instructions"]
    assert "web-scraping" in by_name["w1"]["instructions"]
    assert {goal for goal, _, _ in runner.calls} == {
        "Deep research on: Tell me about Alpha and Beta"
    }
    assert any("Worker w3 failed" in line for line in lines)
    assert any("2 dropped (quote not on its page)" in line for line in lines)


def test_when_writing_fails_the_findings_are_the_report():
    ctx, _ = context(FakeRunner(write_fails=True))
    report = recipe.research("Alpha and Beta?", "quick", ctx)
    stats = report["stats"]
    assert stats["write"] == "failed: GLM is out of quota"
    assert stats["fact_check"] == "skipped"
    assert report["markdown"].startswith("_Writing the report failed")
    assert "## Alpha finances" in report["markdown"]


def test_a_run_whose_workers_all_fail_says_so():
    ctx, _ = context(FakeRunner(workers_fail=True))
    with pytest.raises(RuntimeError, match="Every worker failed"):
        recipe.research("Alpha and Beta?", "quick", ctx)


def test_a_failed_planning_fails_the_run():
    def delegate(goal, tasks, span):
        return {"status": "failed", "error": "no key", "tasks": [], "cost": 0.0}

    ctx, _ = context(delegate)
    with pytest.raises(RuntimeError, match="no key"):
        recipe.research("Alpha?", "quick", ctx)


def test_reply_json_takes_the_last_block_and_falls_back_to_prose():
    text = 'Like ```json\n{"x": 1}\n``` then\n```json\n{"x": 2}\n```\n'
    assert recipe.reply_json(text) == {"x": 2}
    assert recipe.reply_json('So: {"x": 3} done') == {"x": 3}
    assert recipe.reply_json('```json\n{"x": 4}\n```\n```\nnot json\n```') == {"x": 4}
    with pytest.raises(ValueError):
        recipe.reply_json("no JSON here")


class FakeSocket:
    def __init__(self, waits, fail_wait=False):
        self.waits = list(waits)
        self.fail_wait = fail_wait
        self.calls = []

    def __call__(self, socket, op, args, timeout, *, name):
        self.calls.append((op, args))
        if op == "delegate":
            return {"run_id": "dg-1", "queued": 0, "card": ""}
        if op == "wait":
            if self.fail_wait:
                raise RunnerError("The agents runner closed the connection.")
            return self.waits.pop(0)
        if op == "cancel":
            return {"run_id": args["run_id"], "cancelled": True}
        raise AssertionError(op)


def test_agents_runner_follows_a_delegation_and_moves_the_meter():
    lines, meter = [], []
    socket = FakeSocket(
        [
            {
                "events": ["a: started."],
                "done": False,
                "fraction": None,
                "result": None,
            },
            {"events": ["a: done."], "done": False, "fraction": 0.5, "result": None},
            {"events": [], "done": True, "fraction": 1.0, "result": {"status": "ok"}},
        ]
    )
    delegate = recipe.AgentsRunner(Path("/s"), lines.append, meter.append, socket)
    result = delegate("g", [{"name": "a"}], (0.2, 0.6))
    assert result == {"status": "ok", "run_id": "dg-1"}
    assert lines == ["[agents] a: started.", "[agents] a: done."]
    assert meter == pytest.approx([0.4, 0.6])
    assert [args.get("since") for op, args in socket.calls if op == "wait"] == [0, 1, 2]
    assert not any(op == "cancel" for op, _ in socket.calls)


def test_agents_runner_cancels_a_delegation_it_lost_track_of():
    socket = FakeSocket([], fail_wait=True)
    delegate = recipe.AgentsRunner(Path("/s"), lambda m: None, lambda f: None, socket)
    with pytest.raises(RunnerError):
        delegate("g", [{"name": "a"}], (0, 1))
    assert socket.calls[-1] == ("cancel", {"run_id": "dg-1"})


def test_agents_runner_that_isnt_running_says_how_to_start_it():
    def down(*args, **kw):
        raise RunnerError("The agents runner isn't running on the host (…).")

    delegate = recipe.AgentsRunner(Path("/s"), lambda m: None, lambda f: None, down)
    with pytest.raises(RuntimeError, match="make agents-setup"):
        delegate("g", [], (0, 1))
