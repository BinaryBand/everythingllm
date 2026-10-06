"""audit-runner: what the audit tools do, on the host, where the checks can read the
journal and ask every host service on its socket. The MCP server in the container
(server.py) forwards each tool call here over hostrpc and shows the agent the text
returned. Each function in OPS is the tool of the same name; server.py describes them.

Config (environment, from host.env and the unit):
  AUDIT_SOCKET       socket to listen on (default <storage>/audit/runner.sock)
  ANYTHINGLLM_STORAGE  storage directory (default /srv/anythingllm/storage); the defaults
                     below are under it
  AUDIT_JOURNAL_DIR  the host journal (default /var/log/journal)
  ANYTHINGLLM_API    AnythingLLM's API (default http://127.0.0.1:3001/api)
  ANYTHINGLLM_ENV    AnythingLLM's .env, for LLM_PROVIDER and OPENROUTER_API_KEY when
                     they aren't in the environment (default <storage>/.env)
  AUDIT_CREDIT_WARN_USD, AUDIT_CREDIT_FAIL_USD
                     OpenRouter credit under which the audit warns (default 2.00) or
                     fails (default 0.25)
  SEARXNG_URL        the SearXNG search endpoint to test
  SITES_SOURCE, ZOLA and the rest of sites.build's settings
                     the Zola sites, as for sites-runner; the report is written to the
                     status site through the same store and build
  AUDIT_RUNLOGS      where skills write run logs (default <storage>/logs)
"""

import json
import logging
from datetime import datetime, timedelta

import hostrpc
from hostrpc import RunnerError
from sites.build import Builder
from sites.store import SiteError, SiteStore

from audit import checks
from audit.services import WATCHED

# The findings run_checks last numbered, so publish_report publishes the ones the model's
# suggestions refer to. The runner keeps them between calls and runs.
_last: tuple[datetime, list[checks.Finding]] | None = None
REUSE = timedelta(hours=1)


def report_store() -> SiteStore:
    builder = Builder.from_env()
    return SiteStore(builder.source, builder.content, build=builder.build)


def find_job(e: checks.Env, name: str) -> dict:
    jobs = e.api_json("/scheduled-jobs").get("jobs", [])
    found = next((j for j in jobs if j.get("name") == name), None)
    if found is None:
        raise RunnerError(
            f"no scheduled job named '{name}' (jobs: {', '.join(repr(j.get('name')) for j in jobs) or 'none'})."
        )
    return found


def run_checks(since_hours: int = 24) -> str:
    global _last
    e = checks.Env.from_env()
    since = e.now() - timedelta(hours=since_hours)
    findings, left_out = checks.reported(checks.run_all(e, since))
    _last = (e.now(), findings)
    return checks.to_markdown(findings, since, left_out)


def publish_report(
    summary: str, suggestions: dict[str, str] | None = None, status: str | None = None
) -> str:
    if not summary.strip():
        raise RunnerError("summary must be one or two sentences about the findings.")
    # JSON keys are strings; the findings are numbered.
    numbered = {int(n): text for n, text in (suggestions or {}).items()}
    e = checks.Env.from_env()
    rerun = _last is None or e.now() - _last[0] > REUSE
    if rerun:
        findings, _ = checks.reported(checks.run_all(e, e.now() - timedelta(hours=24)))
    else:
        findings = _last[1]
    try:
        entry = checks.publish(
            report_store(), e.today(), findings, summary, numbered, status or ""
        )
    except SiteError as err:
        raise RunnerError(str(err)) from None
    notes = []
    if unknown := sorted(n for n in numbered if not 1 <= n <= len(findings)):
        notes.append(
            f"Ignored suggestions for findings that don't exist: {', '.join(map(str, unknown))}."
        )
    if rerun:
        notes.append(
            "run_checks' findings were over an hour old or missing, so the checks ran again."
        )
    return "\n".join([f"Published {entry.title}: {entry.url}", *notes])


def journal_lines(service: str, since_hours: int = 24, contains: str = "") -> str:
    if service not in WATCHED:
        raise RunnerError(
            f"unknown service '{service}'; use one of: {', '.join(WATCHED)}."
        )
    e = checks.Env.from_env()
    entries = checks.journal_entries(
        e, e.now() - timedelta(hours=since_hours), services=[service]
    )
    if entries is None:
        raise RunnerError(
            f"The host journal can't be read: no journal files under {e.journal_dir}."
        )
    lines = checks.recent_lines(entries, service, contains)
    if not lines:
        return "No matching lines."
    return "\n".join(checks.clip(line, 500) for line in lines)


def job_run(job: str, run_id: int = 0) -> str:
    e = checks.Env.from_env()
    runs = e.api_json(f"/scheduled-jobs/{find_job(e, job)['id']}/runs").get("runs", [])
    run = (
        next((r for r in runs if r.get("id") == run_id), None)
        if run_id
        else (runs[0] if runs else None)
    )
    if run is None:
        raise RunnerError(
            f"no run {run_id} for '{job}'." if run_id else f"'{job}' hasn't run yet."
        )
    result = checks.run_result(run)
    calls = [
        {
            "tool": c.get("toolName"),
            "arguments": checks.clip(json.dumps(c.get("arguments")), 300),
            "result": checks.clip(c.get("result") or "", 600),
        }
        for c in result.get("toolCalls") or []
    ]
    detail = {
        "run": run.get("id"),
        "status": run.get("status"),
        "error": run.get("error"),
        "started": run.get("startedAt"),
        "completed": run.get("completedAt"),
        "seconds": round((result.get("duration") or 0) / 1000),
        "cost": (result.get("metrics") or {}).get("totalCost"),
        "tool_calls": calls,
        "progress": [checks.clip(t, 300) for t in result.get("thoughts") or []][-40:],
        "final_reply": checks.final_line(result),
    }
    if run.get("status") == "timed_out":
        detail["note"] = (
            "AnythingLLM keeps no trace of a timed-out run: no tool calls, progress or reply."
        )
    return json.dumps(detail, ensure_ascii=False, indent=1)


def run_job(name: str) -> str:
    e = checks.Env.from_env()
    job = find_job(e, name)
    status, body = e.post(f"{e.api}/scheduled-jobs/{job['id']}/trigger")
    try:
        reply = json.loads(body)
    except json.JSONDecodeError:
        reply = {}
    reply = reply if isinstance(reply, dict) else {}
    if status not in (200, 201, 202) or reply.get("success") is False:
        why = (
            reply.get("error")
            or reply.get("message")
            or body.decode(errors="replace")
            or "no reply"
        )
        raise RunnerError(
            f"AnythingLLM didn't start '{name}' (status {status or 'none'}): {checks.clip(why, 300)}"
        )
    run = found if isinstance(found := reply.get("run"), dict) else {}
    run_id = run.get("id") or reply.get("runId")
    return (
        f"Started '{name}'"
        + (f", run {run_id}." if run_id else ".")
        + " It runs in the background; job_run shows how it went."
    )


def research_run(question: str = "", index: int = 0, since_hours: int = 24 * 7) -> str:
    e = checks.Env.from_env()
    runs = checks.research_runs_since(e, e.now() - timedelta(hours=since_hours))
    words = question.lower().split()
    matching = [
        r for r in runs if all(w in str(r.get("question", "")).lower() for w in words)
    ]
    if index >= len(matching):
        recent = "\n".join(
            f"- {r.get('started', '')[:16]} {r.get('status')}: {str(r.get('question', ''))[:100]}"
            for r in runs[:10]
        )
        asked = f" matching {question!r}" if words else ""
        raise RunnerError(
            f"no deep-research run{asked} in the last {since_hours} hours. Recent runs:\n{recent or '(none)'}"
        )
    run = dict(matching[index])
    run["events"] = run.get("events", [])[-30:]
    return json.dumps(run, ensure_ascii=False, separators=(",", ":"))


# The checks block (HTTP, journalctl, zola), so hostrpc runs each in a thread.
OPS = (run_checks, publish_report, journal_lines, job_run, run_job, research_run)
runner = hostrpc.Service(OPS, log=logging.getLogger("audit-runner"))


def main() -> None:
    hostrpc.run(runner, "audit", "AUDIT_SOCKET")
