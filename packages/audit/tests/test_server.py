import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path

import hostrpc
import pytest
from audit import checks, server
from audit import tools as audit_tools
from audit.checks import Finding
from hostrpc import RunnerError
from sites.store import SiteStore
from test_checks import API, NOW, job_api, make_site, write_runs


def via_runner(monkeypatch, name, args):
    """The tool as the agent calls it: through the MCP server, over the socket, to the runner."""
    sock = Path("/tmp") / f"audit-test-{os.getpid()}.sock"  # AF_UNIX paths are short
    monkeypatch.setenv("AUDIT_SOCKET", str(sock))

    async def go():
        async with hostrpc.serving(audit_tools.runner, sock):
            return await server.mcp.call_tool(name, args)

    try:
        return asyncio.run(go())
    finally:
        sock.unlink(missing_ok=True)


def test_every_tool_is_an_op_of_the_runner():
    tools = asyncio.run(server.mcp.list_tools())
    assert {t.name for t in tools} == {f.__name__ for f in audit_tools.OPS} - {
        f.__name__ for f in audit_tools.SKILLS
    }


def test_the_ops_that_write_are_skills():
    """Each op in SKILLS has its skill, which sends that op (anythingllm/agent-skills/<op>)."""
    skills = Path(__file__).resolve().parents[3] / "anythingllm" / "agent-skills"
    for op in audit_tools.SKILLS:
        handler = skills / op.__name__.replace("_", "-") / "handler.js"
        assert f'op: "{op.__name__}"' in handler.read_text(), op.__name__


def test_the_server_explains_a_missing_runner(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_SOCKET", str(tmp_path / "nope.sock"))
    with pytest.raises(server.ToolError, match="audit runner isn't running"):
        asyncio.run(server.run_checks())


@pytest.fixture
def tools(env, monkeypatch):
    """The runner's tools on the fake env, with the status site's store (no build)."""
    monkeypatch.setattr(checks.Env, "from_env", classmethod(lambda cls: env))
    monkeypatch.setattr(audit_tools, "_last", None)
    make_site(env, "status", reports="")
    store = SiteStore(env.sites_source, env.sites_content)
    monkeypatch.setattr(audit_tools, "report_store", lambda: store)
    monkeypatch.setattr(
        checks,
        "CHECKS",
        {
            "x": lambda env, since: [
                Finding("info", "search", "SearXNG works"),
                Finding("warn", "sites", "news: no new entry", "Newest is 2026-10-01."),
            ]
        },
    )
    return store


def test_publish_report_reuses_run_checks_findings(env, tools, monkeypatch):
    text = audit_tools.run_checks(24)
    assert (
        "- #1 [sites] news: no new entry" in text
        and "- #2 [search] SearXNG works" in text
    )
    # Checks that changed since don't change what's published: the numbers stay valid.
    monkeypatch.setattr(checks, "CHECKS", {"x": lambda env, since: []})
    env.now = lambda: NOW + timedelta(minutes=5)
    # Called as the publish-report skill does, with JSON's string keys, over the runner's socket.
    sock = Path("/tmp") / f"audit-test-{os.getpid()}.sock"  # AF_UNIX paths are short

    async def go():
        async with hostrpc.serving(audit_tools.runner, sock):
            return await hostrpc.request(
                sock,
                "publish_report",
                {
                    "summary": "The news is late.",
                    "suggestions": {"1": "Run the Daily News Page job.", "7": "?"},
                },
                30,
                name="audit runner",
            )

    try:
        text = asyncio.run(go())
    finally:
        sock.unlink(missing_ok=True)
    assert text.splitlines() == [
        "Published System audit — October 4, 2026 — 1 warning: https://pages/status/reports/2026-10-04/",
        "Ignored suggestions for findings that don't exist: 7.",
    ]
    _, extra, _ = tools.get("status", "reports", "2026-10-04")
    assert [(f["title"], f["suggestion"]) for f in extra["findings"]] == [
        ("news: no new entry", "Run the Daily News Page job."),
        ("SearXNG works", ""),
    ]


def test_publish_report_runs_the_checks_when_it_has_none(env, tools):
    reply = audit_tools.publish_report("Fine.", status="ok")
    # A status passed in sets the page's badge; the title still counts the findings.
    assert reply.splitlines()[0].startswith(
        "Published System audit — October 4, 2026 — 1 warning: "
    )
    assert "checks ran again" in reply
    _, extra, _ = tools.get("status", "reports", "2026-10-04")
    assert extra["status"] == "ok" and len(extra["findings"]) == 2
    with pytest.raises(RunnerError, match="summary"):
        audit_tools.publish_report("  ")


def test_research_run_is_compact_and_recent(env, monkeypatch):
    monkeypatch.setattr(checks.Env, "from_env", classmethod(lambda cls: env))
    write_runs(
        env,
        [
            {
                "started": "2026-10-03T12:00:00Z",
                "question": "q",
                "status": "ok",
                "events": [f"event {i}" for i in range(100)],
            }
        ],
    )
    text = audit_tools.research_run()
    assert "\n" not in text and ", " not in text
    run = json.loads(text)
    assert run["events"] == [f"event {i}" for i in range(70, 100)]


def test_research_run_finds_a_run_by_its_question(env, monkeypatch):
    monkeypatch.setattr(checks.Env, "from_env", classmethod(lambda cls: env))
    write_runs(
        env,
        [
            {
                "started": "2026-10-03T12:00:00Z",
                "question": "How do people teach Bitcoin online?",
                "status": "ok",
            },
            {
                "started": "2026-10-03T13:00:00Z",
                "question": "Sweden jobs",
                "status": "ok",
            },
        ],
    )
    assert json.loads(audit_tools.research_run("bitcoin teach"))["question"].startswith(
        "How do people teach Bitcoin"
    )
    # no words: the newest
    assert json.loads(audit_tools.research_run())["question"] == "Sweden jobs"
    with pytest.raises(
        RunnerError,
        match=r"no deep-research run matching 'quantum'.*\n- 2026-10-03T13:00 ok: Sweden jobs",
    ):
        audit_tools.research_run("quantum")


def test_run_job_triggers_by_name(env, monkeypatch):
    monkeypatch.setattr(checks.Env, "from_env", classmethod(lambda cls: env))
    job_api(
        env,
        [{"id": 4, "name": "Daily News Page"}, {"id": 5, "name": "System Audit"}],
        {},
    )
    posted = []

    def post(url):
        posted.append(url)
        return 200, json.dumps(
            {"success": True, "run": {"id": 23, "status": "running"}}
        ).encode()

    env.post = post
    assert audit_tools.run_job("Daily News Page").startswith(
        "Started 'Daily News Page', run 23."
    )
    assert posted == [f"{API}/scheduled-jobs/4/trigger"]

    env.post = lambda url: (200, b'{"runId": 24}')
    assert "run 24" in audit_tools.run_job("System Audit")

    env.post = lambda url: (
        409,
        b'{"success": false, "error": "Job is already running"}',
    )
    with pytest.raises(
        RunnerError, match="didn't start 'System Audit' .*409.*already running"
    ):
        audit_tools.run_job("System Audit")

    with pytest.raises(
        RunnerError,
        match="no scheduled job named 'News'.*'Daily News Page', 'System Audit'",
    ):
        audit_tools.run_job("News")


def test_job_run_notes_a_timed_out_run_has_no_trace(env, monkeypatch):
    monkeypatch.setattr(checks.Env, "from_env", classmethod(lambda cls: env))
    job_api(
        env,
        [{"id": 7, "name": "News refresh"}],
        {
            7: [
                {
                    "id": 19,
                    "status": "timed_out",
                    "result": None,
                    "error": "Job execution timed out",
                }
            ]
        },
    )
    detail = json.loads(audit_tools.job_run("News refresh"))
    assert detail["status"] == "timed_out" and "no trace" in detail["note"]
