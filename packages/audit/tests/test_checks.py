import asyncio
import contextlib
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from audit import checks
from audit.checks import Finding
from sites.store import SiteStore

NOW = datetime(2026, 10, 4, 4, 0, tzinfo=UTC)
SINCE = NOW - timedelta(hours=24)
API = "http://api/api"
SEARX = "https://searx/search"


def by(findings, severity):
    return [f for f in findings if f.severity == severity]


# --- journal -----------------------------------------------------------------------


def mount_journal(env, entries):
    """Serve (service, message) pairs, each named by its service's WATCHED field."""
    (env.journal_dir / "abc").mkdir()
    (env.journal_dir / "abc" / "user-1001.journal").write_bytes(b"")
    calls = []
    entries = [
        {checks.WATCHED.get(svc, ("CONTAINER_NAME",))[0]: svc, "MESSAGE": msg}
        for svc, msg in entries
    ]

    def run(args):
        calls.append(args)
        return iter([json.dumps(e) + "\n" for e in entries] + ["not json\n"])

    env.run = run
    return calls


def test_no_journal_is_one_warning(env):
    [f] = checks.journal(env, SINCE)
    assert (f.severity, f.area) == ("warn", "logs")
    assert str(env.journal_dir) in f.detail


def test_stream_lines_stops_a_command_that_runs_too_long():
    assert list(
        checks.stream_lines(["sh", "-c", "echo one; exec sleep 5"], timeout=0.5)
    ) == ["one\n"]


# --- host services ---------------------------------------------------------------


def test_runners_all_answering_is_one_info_finding(env):
    asked = []
    env.pings = lambda storage: asked.append(storage) or {}
    [f] = checks.runners(env, SINCE)
    assert (f.severity, f.area) == ("info", "services") and asked == [env.storage]


def test_runners_down_fail_with_each_named(env):
    env.pings = lambda storage: {
        "sites-runner": "The sites-runner isn't running on the host.",
        "podcasts-runner": "The podcasts-runner didn't answer within 20s.",
    }
    [f] = checks.runners(env, SINCE)
    assert (f.severity, f.title) == (
        "fail",
        "2 host service(s) don't answer or aren't ready",
    )
    assert "sites-runner: The sites-runner isn't running on the host." in f.evidence


def test_ping_all_asks_every_service_at_once(tmp_path):
    import hostrpc

    class Unready(hostrpc.Service):
        async def op_ping(self):
            return {"problems": ["no image", "no network"]}

    async def main():
        async with contextlib.AsyncExitStack() as stack:
            for folder, service in (
                ("sites", hostrpc.Service()),
                ("sandbox", Unready()),
            ):
                sock = (
                    Path("/tmp") / f"audit-{folder}-{os.getpid()}.sock"
                )  # AF_UNIX paths are short
                (tmp_path / "everythingllm" / folder).mkdir(parents=True)
                (tmp_path / "everythingllm" / folder / "runner.sock").symlink_to(sock)
                await stack.enter_async_context(hostrpc.serving(service, sock))
            return await asyncio.to_thread(checks.ping_all, tmp_path, 1)

    down = asyncio.run(main())
    assert set(down) == set(checks.RUNNERS) - {"sites-runner"}
    assert down["sandbox-runner"] == "no image; no network"
    assert "research-runner isn't running" in down["research-runner"]


def test_journal_groups_our_errors_and_skips_noise(env):
    app, sx = "systemd-anythingllm", "systemd-searxng"
    calls = mount_journal(
        env,
        [
            (app, "\x1b[31m[MCP] error: sites crashed with code 1\x1b[0m"),
            (app, "[MCP] error: sites crashed with code 2"),
            (app, "Server listening on port 3001"),
            (sx, "WARNING:searx.engines.brave: ErrorContext(...)"),
            (sx, "WARNING:searx.network.duckduckgo: HTTP Request failed"),
            (sx, "WARNING:searx.engines.brave: ErrorContext(...)"),
            (sx, "yandex.py:1: DeprecationWarning: utcfromtimestamp"),
            ("systemd-ollama", "error: slot idle"),
            ("podcasts-web.service", "Traceback (most recent call last):"),
            (app, ["binary", "message"]),
        ],
    )
    findings = checks.journal(env, SINCE)
    args = calls[0]
    assert args[:3] == ["journalctl", "-D", str(env.journal_dir)]
    assert "2026-10-03 04:00:00 UTC" in args
    assert "--output-fields=MESSAGE,CONTAINER_NAME,_SYSTEMD_USER_UNIT" in args
    # journalctl filters to our services itself: matches joined by "+".
    matches = [f"{field}={svc}" for svc, (field, _) in checks.WATCHED.items()]
    assert (
        args[-(2 * len(matches) - 1) :] == [x for m in matches for x in (m, "+")][:-1]
    )
    assert "CONTAINER_NAME=systemd-anythingllm" in args
    warns = by(findings, "warn")
    assert [(f.title.split(":")[0], f.detail.split(" ")[0]) for f in warns] == [
        ("AnythingLLM", "2"),  # numbers normalized, so both crash lines group
        ("podcasts-web", "1"),
    ]
    [engines] = [f for f in findings if f.area == "search"]
    assert engines.detail == "brave 2, duckduckgo 1"
    assert not any("ollama" in f.title or "Deprecation" in f.title for f in findings)


def test_journal_judges_the_line_that_starts_a_message_and_structured_levels(env):
    caddy, app = "systemd-static_agent", "systemd-anythingllm"
    mount_journal(
        env,
        [
            (caddy, '{"level":"warn","msg":"HTTP/3 skipped because it requires TLS"}'),
            (caddy, '{"level":"info","msg":"exiting; byeee!! 👋"}'),
            (caddy, '{"level":"error","msg":"dial tcp: connection refused"}'),
            (app, "Error: 402 This request requires more credits"),
            (
                app,
                "    at APIError.generate (/app/server/node_modules/openai/error.js:63:20)",
            ),
            (app, '  "timeout": 120'),
            (app, "  summary: 'Nothing is failing, but SearXNG errors…'"),
            (
                app,
                "[10/05/26 09:37:42] INFO     Tool 'research_run' failed: 'Error    server.py:444",
            ),
            (
                app,
                "[YOUTUBEJS][Player]: Failed to extract signature decipher algorithm.",
            ),
        ],
    )
    titles = [f.title for f in by(checks.journal(env, SINCE), "warn")]
    assert sorted(titles) == sorted(
        [
            'pages site (Caddy): {"level":"error","msg":"dial tcp: connection refused"}',
            "AnythingLLM: Error: 402 This request requires more credits",
            "AnythingLLM: [10/05/26 09:37:42] INFO Tool 'research_run' failed: 'Error server.py:444",
        ]
    )


def test_journal_lines_for_one_service_keep_the_last_ones(env):
    calls = mount_journal(
        env,
        [("systemd-searxng", f"line {i} Brave") for i in range(150)]
        + [("systemd-anythingllm", "line brave")],
    )
    entries = checks.journal_entries(env, SINCE, services=["systemd-searxng"])
    assert entries is not None
    lines = checks.recent_lines(entries, "systemd-searxng", "BRAVE")
    assert calls[0][-1] == "CONTAINER_NAME=systemd-searxng" and "+" not in calls[0]
    assert (
        len(lines) == 100
        and lines[0] == "line 50 Brave"
        and lines[-1] == "line 149 Brave"
    )


# --- SearXNG -------------------------------------------------------------------------


def searx_reply(results, down=()):
    return 200, json.dumps(
        {"results": results, "unresponsive_engines": [list(d) for d in down]}
    ).encode()


def test_searxng_with_no_results_fails(env):
    env.pages[f"{SEARX}?q=weather&format=json"] = searx_reply(
        [], [("google", "too many requests")]
    )
    [f] = checks.searxng(env, SINCE)
    assert f.severity == "fail" and "google (too many requests)" in f.evidence[0]


def test_searxng_working_but_engines_down(env):
    env.pages[f"{SEARX}?q=weather&format=json"] = searx_reply(
        [
            {"url": "https://a", "engines": ["yahoo"]},
            {"url": "https://b", "engines": ["yandex", "yahoo"]},
        ],
        [("brave", "unexpected crash")],
    )
    info, warn = checks.searxng(env, SINCE)
    assert (info.severity, info.detail) == ("info", "from yahoo, yandex")
    assert warn.severity == "warn" and warn.detail.startswith(
        "brave (unexpected crash). "
    )
    assert "/srv/searxng/settings.yml" in warn.detail


def test_searxng_unreachable_fails(env):
    env.pages[f"{SEARX}?q=weather&format=json"] = (0, b"Connection refused")
    [f] = checks.searxng(env, SINCE)
    assert f.severity == "fail" and "nothing" in f.title


# --- scheduled jobs ------------------------------------------------------------------


def tool_call(name, text, error=False):
    return {
        "toolName": name,
        "arguments": {},
        "result": json.dumps(
            {"content": [{"type": "text", "text": text}], "isError": error}
        ),
    }


def job_api(env, jobs, runs):
    env.pages[f"{API}/scheduled-jobs"] = (200, json.dumps({"jobs": jobs}).encode())
    for job_id, job_runs in runs.items():
        env.pages[f"{API}/scheduled-jobs/{job_id}/runs"] = (
            200,
            json.dumps({"runs": job_runs}).encode(),
        )


def job(id, name, next_run, last_run=None, enabled=True):
    times = {"nextRunAt": next_run, "lastRunAt": last_run}
    return {"id": id, "name": name, "enabled": enabled} | times


def run(id, started, status="completed", text="", calls=(), thoughts=(), error=None):
    return {
        "id": id,
        "status": status,
        "error": error,
        "startedAt": started,
        "result": json.dumps(
            {
                "text": text,
                "toolCalls": list(calls),
                "thoughts": list(thoughts),
                "metrics": {"totalCost": 0.0123},
                "duration": 29669,
            }
        ),
    }


def test_jobs_findings(env):
    job_api(
        env,
        [
            job(4, "Daily News Page", "2026-10-05T03:00:00Z"),
            job(5, "Weekly", "2026-10-04T02:00:00.000Z"),
            job(6, "Old", "2026-01-01T00:00:00Z", enabled=False),
        ],
        {
            4: [
                run(
                    20,
                    "2026-10-04T03:00:00Z",
                    text="<think>hm</think>Published Daily News — October 4, 2026: https://x",
                    calls=[
                        tool_call("sites-list_entries", "No entries yet."),
                        tool_call(
                            "sites-write_entry",
                            "the change was saved, but the site didn't rebuild",
                            True,
                        ),
                    ],
                ),
                run(
                    19, "2026-10-03T05:00:00Z", status="failed", error="model timed out"
                ),
                run(
                    18,
                    "2026-10-02T03:00:00Z",
                    status="failed",
                    error="too old to report",
                ),
            ],
            5: [
                run(
                    7,
                    "2026-10-03T10:00:00Z",
                    text="No news retrieved; nothing published.",
                ),
                run(
                    8,
                    "2026-10-03T11:00:00Z",
                    text="Let me write it.</｜｜DSML｜｜ calls>",
                ),
            ],
            6: [],
        },
    )
    findings = checks.jobs(env, SINCE)
    titles = [(f.severity, f.title) for f in findings]
    assert ("warn", "Weekly missed its run") in titles
    assert ("fail", "Daily News Page failed") in titles
    assert ("warn", "Daily News Page: 1 tool call(s) failed") in titles
    # Judged by tool calls, not wording: no successful call means no work done.
    assert ("warn", "Weekly made no successful tool calls") in titles
    assert ("warn", "Weekly ended in raw tool-call markup") in titles
    assert not any("progress lines" in t for _, t in titles)
    [done] = [f for f in findings if f.title == "Daily News Page completed"]
    assert "30 s, $0.0123" in done.detail and done.evidence[0].startswith(
        "Published Daily News"
    )
    assert not any("too old" in " ".join(f.evidence) for f in findings)
    assert not any(f.title.startswith("Old") for f in findings)


def test_timed_out_run_says_only_how_long_it_ran(env):
    # Live shape: a timed-out run keeps no result, only a generic error.
    timed_out = {
        "id": 19,
        "status": "timed_out",
        "result": None,
        "error": "Job execution timed out",
        "startedAt": "2026-10-03T17:55:00.695Z",
        "completedAt": "2026-10-03T18:00:07.555Z",
    }
    job_api(env, [{"id": 7, "name": "News refresh", "enabled": True}], {7: [timed_out]})
    [f] = checks.jobs(env, SINCE)
    assert (f.severity, f.title, f.evidence) == ("fail", "News refresh timed out", [])
    assert f.detail == (
        "News refresh, run 19 at 2026-10-03 17:55 UTC; timed out after 307s; "
        "AnythingLLM keeps no trace of a timed-out run."
    )


def test_jobs_that_ran_are_not_missed(env):
    # Live shape: after a run AnythingLLM leaves nextRunAt a millisecond before lastRunAt.
    job_api(
        env,
        [
            job(1, "Morning", "2026-10-04T03:00:00.243Z", "2026-10-04T03:00:00.244Z"),
            job(2, "Ran early", "2026-10-04T02:00:00.500Z"),
            job(3, "Stuck", "2026-10-04T03:00:00.000Z", "2026-10-03T03:00:00.001Z"),
        ],
        {
            1: [],  # the run itself may be listed or not; lastRunAt is enough
            2: [run(9, "2026-10-04T01:59:30.000Z", calls=[tool_call("t", "ok")])],
            3: [run(3, "2026-10-03T03:00:00.010Z", calls=[tool_call("t", "ok")])],
        },
    )
    missed = [f.title for f in checks.jobs(env, SINCE) if "missed" in f.title]
    assert missed == ["Stuck missed its run"]


def test_jobs_api_down_raises(env):
    with pytest.raises(RuntimeError, match="answered 404"):
        checks.jobs(env, SINCE)


# --- deep-research runs ----------------------------------------------------------------


def write_runs(env, runs):
    folder = env.runlogs
    (folder / "2026-10.jsonl").write_text(
        "\n".join(json.dumps(r) for r in runs) + "\nbroken\n"
    )


def test_research_runs(env):
    write_runs(
        env,
        [
            {
                "started": "2026-10-03T12:00:00Z",
                "question": "ok one",
                "status": "ok",
                "published": True,
                "seconds": 300,
                "chat_closed": True,
                "url": "https://r/1",
                "stats": {
                    "sources": 9,
                    "searches": 4,
                    "failed_searches": 0,
                    "fact_check": "ok",
                    "workers_detail": [{"goal": "g", "findings": 5, "stopped": "done"}],
                },
            },
            {
                "started": "2026-10-03T13:00:00Z",
                "question": "troubled",
                "status": "ok",
                "published": False,
                "build_error": "zola build failed",
                "seconds": 1500,
                "stats": {
                    "searches": 10,
                    "failed_searches": 4,
                    "fact_check": "failed: ran out of output tokens",
                    "workers_detail": [
                        {"goal": "dead", "findings": 0, "stopped": "search-down"},
                        {"goal": "empty", "findings": 0, "stopped": "wasted"},
                    ],
                },
            },
            {
                "started": "2026-10-03T14:00:00Z",
                "question": "broke",
                "status": "failed",
                "error": "Web search isn't working",
            },
            {
                "started": "2026-10-03T15:00:00Z",
                "question": "halted",
                "status": "stopped",
            },
            {
                "started": "2026-10-01T15:00:00Z",
                "question": "too old",
                "status": "failed",
            },
        ],
    )
    # An earlier month's log isn't even read.
    (env.runlogs / "2026-09.jsonl").write_text(
        '{"started": "2026-10-03T16:00:00Z", "status": "failed"}\n'
    )
    findings = checks.research_runs(env, SINCE)
    assert [(f.severity, f.title) for f in findings] == [
        ("info", "Deep research run was stopped when its chat closed"),
        ("fail", "Deep research run failed"),
        ("fail", "Deep research report saved but not published"),
        ("warn", "Deep research run had problems"),
        ("info", "Deep research run completed"),
    ]
    problems = findings[3].evidence
    assert problems == [
        "4 of 10 searches failed",
        "fact-check failed: ran out of output tokens",
        "worker stopped because search was down: dead",
        "worker found nothing (wasted): empty",
        "took 25 min",
    ]
    assert findings[4].evidence == [
        "9 sources, 300 s, https://r/1",
        "finished after its chat closed, so the reply never reached the chat",
    ]


# --- sites -----------------------------------------------------------------------------


def make_site(env, name, **sections):
    """A site with these sections, each given the TOML for its _index.md (default: editions)."""
    site = env.sites_source / name
    for section, toml in (sections or {"editions": ""}).items():
        (site / "content" / section).mkdir(parents=True)
        (site / "content" / section / "_index.md").write_text(f"+++\n{toml}+++\n")
    (site / "zola.toml").write_text(
        f'base_url = "https://pages/{name}"\ntitle = "{name}"\n'
    )


EDITION_RULES = (
    '[extra.audit]\nmax_age_days = 1\nrequired = ["sections[].stories[].url"]\n'
)


def test_walk_counts_present_and_missing():
    extra = {
        "sections": [
            {"stories": [{"url": "a"}, {"headline": "x"}, {"url": ""}]},
            {"stories": []},
            {},
        ]
    }
    assert checks.walk(extra, ["sections[]", "stories[]", "url"]) == (1, 3)
    assert checks.walk({"a": {"b": 1}}, ["a", "b"]) == (1, 0)
    assert checks.walk({}, ["a"]) == (0, 1)


def test_sites_links_age_and_required_fields(env):
    make_site(env, "news", editions=EDITION_RULES)
    make_site(env, "quiet")
    store = SiteStore(env.sites_source, env.sites_content)
    store.write(
        "news",
        "editions",
        "2026-10-01",
        "Old",
        "2026-10-01",
        {"sections": [{"stories": [{"url": "https://a"}, {"headline": "no link"}]}]},
    )
    store.write(
        "news",
        "editions",
        "2026-10-02",
        "Newer",
        "2026-10-02",
        {"sections": [{"stories": [{"url": "https://b"}]}]},
    )
    for url in (
        "https://pages/news/",
        "https://pages/news/editions/2026-10-01/",
        "https://pages/quiet/",
    ):
        env.pages[url] = (200, b"ok")
    env.now = lambda: NOW + timedelta(hours=8)  # 14:00 in Stockholm: two days on
    findings = checks.sites(env, SINCE)
    assert [(f.severity, f.title) for f in findings] == [
        ("fail", "news: 1 page(s) don't load"),
        ("warn", "news/editions: no new entry"),
        ("warn", "news/editions: entries missing `sections[].stories[].url`"),
    ]
    # Only each section's newest entry is fetched.
    assert findings[0].evidence == ["https://pages/news/editions/2026-10-02/ → 404"]
    assert "2026-10-02" in findings[1].detail
    assert findings[2].evidence == ["editions/2026-10-01: 1 of 2 missing"]


def test_sites_audit_rules_hold_only_their_section(env):
    make_site(env, "news", editions=EDITION_RULES, articles="")
    store = SiteStore(env.sites_source, env.sites_content)
    store.write(
        "news",
        "editions",
        "2026-10-01",
        "Edition",
        "2026-10-01",
        {"sections": [{"stories": [{"url": "https://b"}]}]},
    )
    store.write(
        "news", "articles", "us-1-2026-10-03", "Article", "2026-10-03", {"desk": "US"}
    )
    for i in range(12):  # more than ten newer articles don't crowd out the edition
        store.write(
            "news", "articles", f"a-{i}", "Article", "2026-10-03", {"desk": "US"}
        )
        env.pages[f"https://pages/news/articles/a-{i}/"] = (200, b"ok")
    for url in ("https://pages/news/", "https://pages/news/editions/2026-10-01/"):
        env.pages[url] = (200, b"ok")
    # Each section's newest page is checked, but the rules only hold editions to them: a newer
    # article doesn't stand in for today's edition, and articles needn't have story links.
    broken, stale = checks.sites(env, SINCE)
    assert broken.evidence == ["https://pages/news/articles/us-1-2026-10-03/ → 404"]
    assert stale.title == "news/editions: no new entry" and "2026-10-01" in stale.detail


def test_sites_age_goes_by_stockholm_days(env):
    # 22:30 UTC on Oct 4 is already Oct 5 in Stockholm: Oct 4's edition is one day old,
    # not zero.
    make_site(env, "news", editions=EDITION_RULES)
    SiteStore(env.sites_source, env.sites_content).write(
        "news",
        "editions",
        "2026-10-04",
        "Edition",
        "2026-10-04",
        {"sections": [{"stories": [{"url": "u"}]}]},
    )
    env.pages["https://pages/news/"] = env.pages[
        "https://pages/news/editions/2026-10-04/"
    ] = (200, b"ok")
    env.now = lambda: datetime(2026, 10, 4, 22, 30, tzinfo=UTC)
    assert env.today().isoformat() == "2026-10-05"
    assert checks.sites(env, SINCE) == []


def test_sites_flags_entries_saved_after_the_last_build(env, tmp_path):
    make_site(env, "news")
    make_site(env, "quiet")
    store = SiteStore(env.sites_source, env.sites_content)
    store.write("news", "editions", "2026-10-03", "Edition", "2026-10-03", {})
    store.write("quiet", "editions", "2026-10-03", "Edition", "2026-10-03", {})
    env.sites_output = tmp_path / "site"
    for name in ("news", "quiet"):
        (env.sites_output / name).mkdir(parents=True)
        (env.sites_output / name / ".zola-site").write_text("built")
        env.pages[f"https://pages/{name}/"] = env.pages[
            f"https://pages/{name}/editions/2026-10-03/"
        ] = (200, b"ok")
    built = env.sites_output / "news" / ".zola-site"
    # an overwrite saved an hour after the build
    os.utime(built, (built.stat().st_mtime - 3600,) * 2)
    [f] = checks.sites(env, SINCE)
    assert (f.severity, f.title) == (
        "fail",
        "news: entries changed since the last build",
    )
    assert f.evidence[0].startswith("editions/2026-10-03 saved ")

    (env.sites_output / "quiet" / ".zola-site").unlink()
    titles = [f.title for f in checks.sites(env, SINCE)]
    assert "quiet: site was never built" in titles


# --- everything --------------------------------------------------------------------------


def test_run_all_reports_a_crashed_check_and_sorts_by_severity(env, monkeypatch):
    def boom(env, since):
        raise ValueError("bad data")

    monkeypatch.setattr(
        checks,
        "CHECKS",
        {
            "a": lambda env, since: [Finding("info", "a", "fine")],
            "b": boom,
            "c": lambda env, since: [
                Finding("fail", "c", "broken", "detail", ["evidence"])
            ],
        },
    )
    findings = checks.run_all(env, SINCE)
    assert [(f.severity, f.title) for f in findings] == [
        ("fail", "broken"),
        ("warn", "The b check itself failed"),
        ("info", "fine"),
    ]
    text = checks.to_markdown(findings, SINCE)
    assert (
        text.splitlines()[0]
        == "# Audit since 2026-10-03 04:00 UTC: 1 fail, 1 warn, 1 info"
    )
    assert "- #1 [c] broken — detail\n  - evidence" in text


def test_reported_keeps_fail_and_warn_and_five_info_across_areas():
    findings = [
        Finding("fail", "jobs", "f", "d", ["e"]),
        Finding("warn", "sites", "w", "d", ["e"]),
    ]
    findings += [
        Finding(
            "info", "jobs", "Podcasts completed", f"run {i} " + "x" * 300, ["evidence"]
        )
        for i in range(4)
    ]
    findings += [
        Finding("info", "jobs", "News completed", f"run {i}") for i in range(2)
    ]
    findings += [Finding("info", "search", "engines"), Finding("info", "llm", "credit")]
    kept, left_out = checks.reported(findings)
    # Every fail and warn; info taken an area at a time and within one a title at a time,
    # kept in their order.
    assert [(f.title, f.detail[:5]) for f in kept[2:]] == [
        ("Podcasts completed", "run 0"),
        ("Podcasts completed", "run 1"),
        ("News completed", "run 0"),
        ("engines", ""),
        ("credit", ""),
    ]
    assert left_out == 3
    text = checks.to_markdown(kept, SINCE, left_out)
    assert (
        "1 fail, 1 warn, 5 info (and 3 more info, left out of the report)"
        in text.splitlines()[0]
    )
    assert "- #2 [sites] w — d\n  - e" in text
    # Info in one line: detail clipped, no evidence.
    [run0] = [line for line in text.splitlines() if "run 0 x" in line]
    assert (
        run0.startswith("- #3 [jobs] Podcasts completed — run 0 ") and len(run0) < 220
    )
    assert "evidence" not in text and text.endswith("- #7 [llm] credit")


def test_report_title_says_what_was_found():
    def title(*severities):
        return checks.report_title(
            date(2026, 10, 3), [Finding(s, "x", s[0], "") for s in severities]
        )

    passed = "System audit — October 3, 2026 — all checks passed"
    assert title() == title("info") == passed
    assert title("warn", "warn").endswith(" — 2 warnings")
    assert title("fail").endswith(" — 1 failing")


def test_report_extra_and_publish(env):
    make_site(env, "status", reports="")
    store = SiteStore(env.sites_source, env.sites_content)
    findings = [
        Finding("fail", "jobs", "News timed out", "run 19; timed out after 307s."),
        Finding("warn", "search", "2 engines refusing", "brave, google.", ["a", "b"]),
        Finding("info", "llm", "OpenRouter credit: $5.00 left", "on the account"),
    ]
    entry = checks.publish(
        store,
        env.today(),
        findings,
        " Search is  degraded. ",
        {2: "Disable brave.", 9: "x"},
    )
    # 04:00 UTC on Oct 4 is 06:00 on Oct 4 in Stockholm.
    assert (entry.slug, entry.date, entry.title) == (
        "2026-10-04",
        "2026-10-04",
        "System audit — October 4, 2026 — 1 failing, 1 warning",
    )
    assert entry.url == "https://pages/status/reports/2026-10-04/"
    _, extra, body = store.get("status", "reports", "2026-10-04")
    assert not body.strip()
    keys = ("severity", "area", "title", "detail", "suggestion")
    assert extra == {
        "status": "fail",
        "summary": "Search is degraded.",
        "findings": [
            dict(zip(keys, f))
            for f in [
                ("fail", "jobs", "News timed out", "run 19; timed out after 307s.", ""),
                (
                    "warn",
                    "search",
                    "2 engines refusing",
                    "brave, google. — a; b",
                    "Disable brave.",
                ),
                ("info", "llm", "OpenRouter credit: $5.00 left", "on the account", ""),
            ]
        ],
    }
    assert checks.report_extra(findings[2:], "s", {})["status"] == "ok"
    assert checks.report_extra(findings[1:], "s", {}, status="fail")["status"] == "fail"


# --- OpenRouter credit -----------------------------------------------------------------


def openrouter(env, key=(200, {}), credits=(404, {})):
    """Fake OpenRouter: answers /key and /credits, recording the headers sent."""
    sent = []

    def http(url, headers=None):
        sent.append((url, headers))
        status, data = {
            f"{checks.OPENROUTER}/key": key,
            f"{checks.OPENROUTER}/credits": credits,
        }[url]
        return status, json.dumps(data).encode() if isinstance(data, dict) else data

    env.http = http
    env.settings = {"LLM_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "sk-or-secret"}
    return sent


def test_credit_skipped_off_openrouter_or_without_a_key(env):
    env.settings = {"LLM_PROVIDER": "ollama", "OPENROUTER_API_KEY": "sk-or-secret"}
    [f] = checks.credit(env, SINCE)
    assert (f.severity, f.title) == (
        "info",
        "OpenRouter credit not checked",
    ) and "ollama" in f.detail
    env.settings = {"LLM_PROVIDER": "openrouter"}
    [f] = checks.credit(env, SINCE)
    assert f.severity == "info" and "No OPENROUTER_API_KEY" in f.detail


def test_credit_levels(env):
    sent = openrouter(
        env,
        key=(200, {"data": {"limit_remaining": None, "usage": 40}}),
        credits=(200, {"data": {"total_credits": 50, "total_usage": 49.9}}),
    )
    [f] = checks.credit(env, SINCE)
    assert (f.severity, f.title) == ("fail", "OpenRouter credit is nearly gone")
    assert f.detail.startswith("$0.10 left on the account.")
    assert sent[0] == (
        f"{checks.OPENROUTER}/key",
        {"Authorization": "Bearer sk-or-secret"},
    )

    # The key's own limit counts when it's lower than the account's balance.
    openrouter(
        env,
        key=(200, {"data": {"limit_remaining": 1.5}}),
        credits=(200, {"data": {"total_credits": 50, "total_usage": 10}}),
    )
    [f] = checks.credit(env, SINCE)
    assert (f.severity, f.detail[:31]) == ("warn", "$1.50 left on the key's limit. ")
    assert "131072" in f.detail

    openrouter(env, key=(200, {"data": {"limit_remaining": 12.345}}))  # no /credits
    [f] = checks.credit(env, SINCE)
    assert (f.severity, f.title) == ("info", "OpenRouter credit: $12.35 left")

    env.credit_warn_usd = 20
    [f] = checks.credit(env, SINCE)
    assert f.severity == "warn"


def test_credit_never_shows_the_key(env):
    openrouter(
        env, key=(401, b'{"error": "No auth credentials found for sk-or-secret"}')
    )
    [f] = checks.credit(env, SINCE)
    assert f.severity == "warn" and "401" in f.title
    assert "sk-or-secret" not in f.detail and "<key>" in f.detail
    assert "sk-or-secret" not in repr(env)


def test_read_settings_prefers_the_environment(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "LLM_PROVIDER='openrouter'\nOPENROUTER_API_KEY=sk-or-file\nSIG_KEY=other\n"
    )
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    assert checks.read_settings(dotenv) == {
        "LLM_PROVIDER": "openrouter",
        "OPENROUTER_API_KEY": "sk-or-env",
    }
    assert checks.read_settings(tmp_path / "missing") == {
        "LLM_PROVIDER": "",
        "OPENROUTER_API_KEY": "sk-or-env",
    }


def test_research_runs_in_progress_or_interrupted(env):
    write_runs(
        env,
        [
            {
                "started": "2026-10-03T12:00:00Z",
                "question": "done",
                "status": "ok",
                "stats": {"sources": 1},
            }
        ],
    )
    running = env.runlogs / "running"
    running.mkdir()
    for name, started, quiet in (
        ("live", "2026-10-03T23:50:00Z", 2),
        # fresh a moment ago, but no longer touched
        ("restarted", "2026-10-03T23:55:00Z", 4),
        ("quiet", "2026-10-03T22:00:00Z", 100),
    ):
        f = running / f"{name}.json"
        f.write_text(
            json.dumps(
                {
                    "started": started,
                    "question": name,
                    "depth": "standard",
                    "stale_ms": 180_000,
                }
            )
        )
        t = (NOW - timedelta(minutes=quiet)).timestamp()
        os.utime(f, (t, t))
    runs = checks.research_runs_since(env, SINCE)
    assert [(r["question"], r["status"]) for r in runs] == [
        ("restarted", "interrupted"),
        ("live", "running"),
        ("quiet", "interrupted"),
        ("done", "ok"),
    ]
    findings = checks.research_runs(env, SINCE)
    assert [(f.severity, f.title) for f in findings[:2]] == [
        ("warn", "Deep research run was interrupted"),
        ("info", "Deep research run in progress"),
    ]
