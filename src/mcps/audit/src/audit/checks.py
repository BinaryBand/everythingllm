"""Fixed health checks over the AnythingLLM setup.

Each check returns Findings (fail / warn / info). They only report what they
observe; the System Audit job's model summarizes them and suggests fixes, and
publish() writes them as the day's report on the status site.
Everything a check touches (commands, HTTP, the clock, paths) comes from Env,
so tests run offline.
"""

import asyncio
import json
import os
import re
import subprocess
import threading
import urllib.error
import urllib.request
from collections import Counter, deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import hostrpc
from hostrpc import RunnerError
from sites.build import MARKER, Builder
from sites.store import STOCKHOLM, Entry, SiteError, SiteStore

from audit.services import RUNNERS, WATCHED

SEVERITIES = ("fail", "warn", "info")
MAX_INFO = 5  # info findings a report keeps
# The checks all run in one tool call, which AnythingLLM gives up on after 60 s.
COMMAND_SECONDS = 20  # journalctl
PING_SECONDS = 20  # a host service's ping; the sandbox runner's checks podman too

TROUBLE = re.compile(
    r"error|exception|traceback|fatal|panic|crash|failed|denied|timed? ?out|warn",
    re.IGNORECASE,
)
# Lines that look like trouble but aren't worth a report.
NOISE_PATTERNS = (
    r"DeprecationWarning|utcfromtimestamp",
    r"can't register engine \(loading engine failed\)",
    r"limiter\.toml",
    r"X-Forwarded-For nor X-Real-IP",
    r"add_unresponsive_engine after ResultContainer\.close",
    r"filter_urls \(field 'url'\)",
    r"prisma:info",
    r"\[YOUTUBEJS\]",  # AnythingLLM's YouTube loader, after each YouTube change
)
NOISE = re.compile("|".join(NOISE_PATTERNS), re.IGNORECASE)
# SearXNG logs one line per failing engine request; these become counts per engine.
SEARX_ENGINE = re.compile(r"searx\.(?:engines|network)\.([\w ]+?):")
# Where to change SearXNG, for the model's suggestions: not in this repo.
SEARXNG_HOME = (
    "SearXNG's engines are set in /srv/searxng/settings.yml, deployed by Ansible."
)
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# DeepSeek's tool-call markup (fullwidth bars) left in a final reply: the model stopped
# mid tool call, so the run's last step never happened.
TOOL_MARKUP = re.compile(r"｜DSML｜|<｜[^｜]*｜>|</?tool_calls?>")
# Reports and site entries are dated by the user's time zone (the checks run on the host,
# which has the tz database).
OPENROUTER = "https://openrouter.ai/api/v1"
# AnythingLLM's settings the checks read (from its .env); nothing else is loaded.
SETTINGS = ("LLM_PROVIDER", "OPENROUTER_API_KEY")


@dataclass
class Finding:
    severity: str  # fail | warn | info
    area: str
    title: str
    detail: str = ""
    evidence: list[str] = field(default_factory=list)


def http(
    url: str,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 15,
) -> tuple[int, bytes]:
    """Status and body; status 0 when the server couldn't be reached. With `data`, a POST
    of that JSON."""
    kind = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(
        url, data, headers={"User-Agent": "anything-audit", **kind, **(headers or {})}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, str(getattr(e, "reason", e)).encode()


def read_settings(env_file: Path, names: Iterable[str] = SETTINGS) -> dict[str, str]:
    """These keys from the environment, else from AnythingLLM's .env (an MCP server only
    gets the env set for it in mcp_servers.json); "" for one found nowhere."""
    found = hostrpc.env_values(env_file, names := tuple(names))
    return {n: found.get(n, "") for n in names}


def stream_lines(args: list[str], timeout: float = COMMAND_SECONDS) -> Iterator[str]:
    """A command's output line by line, without holding all of it in memory. The command
    is killed after `timeout` seconds, which ends the output early."""
    with subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    ) as proc:
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        try:
            assert proc.stdout is not None  # stdout=PIPE
            yield from proc.stdout
        finally:
            timer.cancel()


def ping_all(storage: Path, timeout: float = PING_SECONDS) -> dict[str, str]:
    """The host services in RUNNERS that don't answer `ping` on their sockets, or answer
    with problems (the sandbox runner checks its image, network and proxy), with why.
    They're all asked at once, so a hung one costs `timeout`, not one each."""

    async def ping(name: str, folder: str) -> str:
        try:
            reply = await hostrpc.request(
                storage / folder / "runner.sock", "ping", {}, timeout, name=name
            )
        except RunnerError as e:
            return str(e)
        return "; ".join(reply.get("problems") or [])

    async def everyone() -> list[str]:
        return await asyncio.gather(
            *(ping(name, folder) for name, folder in RUNNERS.items())
        )

    return {name: why for name, why in zip(RUNNERS, asyncio.run(everyone())) if why}


@dataclass
class Env:
    journal_dir: Path
    api: str
    searxng_url: str
    sites_source: Path
    sites_content: Path
    runlogs: Path
    sites_output: Path | None = None  # built sites; None skips the stale-build check
    settings: dict[str, str] = field(
        default_factory=dict, repr=False
    )  # holds the OpenRouter key
    credit_warn_usd: float = (
        2.0  # about what a 131072-token reply costs on a pricier model
    )
    credit_fail_usd: float = 0.25
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    http: Callable[..., tuple[int, bytes]] = http  # (url, headers=None)
    post: Callable[[str], tuple[int, bytes]] = lambda url: http(
        url, data=b"{}", timeout=30
    )
    run: Callable[[list[str]], Iterable[str]] = stream_lines
    storage: Path = field(
        default_factory=hostrpc.storage
    )  # where the host services' sockets are
    pings: Callable[[Path], dict[str, str]] = (
        ping_all  # storage -> the services down, with why
    )

    @classmethod
    def from_env(cls) -> "Env":
        sites = Builder.from_env()  # the same sites sites-runner writes
        storage = hostrpc.storage()
        return cls(
            journal_dir=Path(os.environ.get("AUDIT_JOURNAL_DIR", "/var/log/journal")),
            api=os.environ.get("ANYTHINGLLM_API", "http://127.0.0.1:3001/api").rstrip(
                "/"
            ),
            searxng_url=os.environ.get("SEARXNG_URL", ""),
            sites_source=sites.source,
            sites_content=sites.content,
            sites_output=sites.output,
            runlogs=Path(os.environ.get("AUDIT_RUNLOGS", storage / "logs")),
            settings=read_settings(
                Path(os.environ.get("ANYTHINGLLM_ENV", storage / ".env"))
            ),
            storage=storage,
            credit_warn_usd=float(os.environ.get("AUDIT_CREDIT_WARN_USD", "2.0")),
            credit_fail_usd=float(os.environ.get("AUDIT_CREDIT_FAIL_USD", "0.25")),
        )

    def today(self) -> date:
        """Today in Stockholm, the date reports and site entries go by."""
        return self.now().astimezone(STOCKHOLM).date()

    def api_json(self, path: str):
        status, body = self.http(f"{self.api}{path}")
        if status != 200:
            raise RuntimeError(
                f"AnythingLLM API {path} answered {status or 'nothing'}: {body[:200].decode(errors='replace')}"
            )
        return json.loads(body)


def clip(text: str, n: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


# --- journal -------------------------------------------------------------------


def journal_entries(
    env: Env, since: datetime, services: Iterable[str] = WATCHED
) -> Iterator[dict] | None:
    """The services' journal entries since `since`, streamed; None when there's no journal to read.

    journalctl does the filtering (one match per service, OR-ed with "+") and sends
    only the fields used, so a day of a busy host's journal never sits in memory.
    """
    if not any(env.journal_dir.glob("*/*.journal")):
        return None
    matches = []
    for service in services:
        matches += [f"{WATCHED[service][0]}={service}", "+"]
    args = [
        "journalctl",
        "-D",
        str(env.journal_dir),
        "-o",
        "json",
        "--no-pager",
        "--output-fields=MESSAGE,CONTAINER_NAME,_SYSTEMD_USER_UNIT",
        "--since",
        since.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        *matches[:-1],
    ]

    def entries() -> Iterator[dict]:
        for line in env.run(args):
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            service = e.get("CONTAINER_NAME") or e.get("_SYSTEMD_USER_UNIT")
            message = e.get("MESSAGE")
            if service in WATCHED and isinstance(message, str):
                # Leading space kept: it marks a continuation line (see trouble()).
                yield {"service": service, "message": ANSI.sub("", message).rstrip()}

    return entries()


def normalize(message: str) -> str:
    """The same complaint with different numbers, IDs or times groups together."""
    m = re.sub(
        r"\b[0-9a-f]{8,}\b|\b[0-9a-f-]{36}\b", "<id>", message, flags=re.IGNORECASE
    )
    m = re.sub(r"\d+", "#", m)
    return clip(m, 200)


# Levels a structured (JSON) log line has to have to count.
BAD_LEVELS = {"error", "fatal", "panic", "dpanic", "critical"}


def trouble(message: str) -> bool:
    """Whether a log line reports a problem.

    A line that starts with whitespace continues the one before it: a stack frame, a
    wrapped log line, a piece of a printed object (tool payloads, which quote earlier
    audit reports back at it). The line that starts it is judged instead. A JSON line
    with a `level` (Caddy) goes by that level, not by words in it ("level":"warn")."""
    if not message or message[0].isspace() or NOISE.search(message):
        return False
    if message.startswith("{"):
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and isinstance(level := data.get("level"), str):
            return level.lower() in BAD_LEVELS
    return bool(TROUBLE.search(message))


def journal(env: Env, since: datetime) -> list[Finding]:
    entries = journal_entries(env, since)
    if entries is None:
        return [
            Finding(
                "warn",
                "logs",
                "The host journal can't be read",
                f"No journal files under {env.journal_dir}, so service logs weren't checked. The journal "
                "may not be persistent (Storage= in journald.conf), or AUDIT_JOURNAL_DIR points elsewhere.",
            )
        ]
    groups: dict[tuple[str, str], tuple[int, str]] = {}  # -> (count, latest message)
    engines: Counter[str] = Counter()
    for e in entries:
        message = e["message"]
        if not trouble(message):
            continue
        message = message.strip()
        if e["service"] == "systemd-searxng" and (m := SEARX_ENGINE.search(message)):
            engines[m.group(1).strip()] += 1
            continue
        key = (e["service"], normalize(message))
        groups[key] = (groups.get(key, (0, ""))[0] + 1, message)

    findings = []
    ranked = sorted(groups.items(), key=lambda kv: kv[1][0], reverse=True)
    for (service, _), (count, latest) in ranked[:15]:
        findings.append(
            Finding(
                "warn",
                "logs",
                f"{WATCHED[service][1]}: {clip(latest, 120)}",
                f"{count} time(s) since {since:%Y-%m-%d %H:%M} UTC.",
                [clip(latest, 500)],
            )
        )
    if len(ranked) > 15:
        findings.append(
            Finding(
                "info",
                "logs",
                f"{len(ranked) - 15} more kinds of log errors not listed",
                "Use journal_lines to look at a service's log.",
            )
        )
    if engines:
        findings.append(
            Finding(
                "info",
                "search",
                "SearXNG engine errors in its log",
                ", ".join(f"{name} {n}" for name, n in engines.most_common()),
            )
        )
    return findings


# --- SearXNG -------------------------------------------------------------------


def searxng(env: Env, since: datetime) -> list[Finding]:
    if not env.searxng_url:
        return [
            Finding(
                "warn",
                "search",
                "SEARXNG_URL isn't set",
                "The audit can't test web search.",
            )
        ]
    status, body = env.http(f"{env.searxng_url}?q=weather&format=json")
    if status != 200:
        return [
            Finding(
                "fail",
                "search",
                f"SearXNG answered {status or 'nothing'}",
                clip(body.decode(errors="replace")),
                [env.searxng_url],
            )
        ]
    data = json.loads(body)
    results = data.get("results") or []
    down = [f"{name} ({why})" for name, why in data.get("unresponsive_engines") or []]
    used = sorted({e for r in results for e in r.get("engines", [])})
    if not results:
        return [
            Finding(
                "fail",
                "search",
                "SearXNG returns no results",
                f"Web search, deep research and the news job can't work. {SEARXNG_HOME}",
                [f"engines refusing: {', '.join(down) or 'none listed'}"],
            )
        ]
    findings = [
        Finding(
            "info",
            "search",
            f"SearXNG works: {len(results)} results",
            f"from {', '.join(used) or 'unknown engines'}",
        )
    ]
    if down:
        findings.append(
            Finding(
                "warn",
                "search",
                f"{len(down)} SearXNG engine(s) refusing requests",
                f"{', '.join(down)}. {SEARXNG_HOME}",
            )
        )
    return findings


# --- host services -----------------------------------------------------------------


def runners(env: Env, since: datetime) -> list[Finding]:
    """Every host service the MCP servers and skills hand work to answers on its socket,
    and has no problems to report."""
    down = [f"{name}: {why}" for name, why in env.pings(env.storage).items()]
    if down:
        return [
            Finding(
                "fail",
                "services",
                f"{len(down)} host service(s) don't answer or aren't ready",
                "Their MCP tools or skills fail until they're back. "
                "Check `systemctl --user status <name>` and its log.",
                down,
            )
        ]
    return [
        Finding(
            "info",
            "services",
            f"All {len(RUNNERS)} host services answer",
            ", ".join(RUNNERS),
        )
    ]


# --- scheduled jobs --------------------------------------------------------------


def run_result(run: dict) -> dict:
    """A job run's execution trace: text, toolCalls, thoughts, metrics, duration."""
    try:
        result = json.loads(run.get("result") or "{}")
    except json.JSONDecodeError:
        return {}
    return result if isinstance(result, dict) else {}


def final_line(result: dict) -> str:
    text = re.sub(
        r"<think>.*?</think>", "", result.get("text") or "", flags=re.DOTALL
    ).strip()
    return text.splitlines()[-1].strip() if text else ""


def tool_outcomes(result: dict) -> list[tuple[str, bool, str]]:
    """(tool name, succeeded, result text) for each tool call in a run."""
    out = []
    for call in result.get("toolCalls") or []:
        raw = call.get("result")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            text = " ".join(
                c.get("text", "")
                for c in parsed.get("content") or []
                if isinstance(c, dict)
            )
            out.append((call.get("toolName"), not parsed.get("isError"), text))
        else:
            out.append((call.get("toolName"), True, str(raw or "")))
    return out


def run_findings(name: str, run: dict, started: datetime) -> list[Finding]:
    """What one job run did, judged by its status and tool calls rather than its wording."""
    when = f"{name}, run {run.get('id')} at {started:%Y-%m-%d %H:%M} UTC"
    status = run.get("status")
    if status == "timed_out":
        # AnythingLLM keeps nothing of such a run (no tool calls, progress or reply), so
        # there's nothing to say about why; only how long it ran.
        done = parse_time(run.get("completedAt"))
        took = f" after {(done - started).total_seconds():.0f}s" if done else ""
        return [
            Finding(
                "fail",
                "jobs",
                f"{name} timed out",
                f"{when}; timed out{took}; AnythingLLM keeps no trace of a timed-out run.",
            )
        ]
    if status == "failed":
        return [
            Finding(
                "fail",
                "jobs",
                f"{name} failed",
                when,
                [clip(run.get("error") or "no error recorded", 500)],
            )
        ]
    if status != "completed":
        return []
    result = run_result(run)
    calls = tool_outcomes(result)
    last = final_line(result)
    findings = []
    failed = [f"{tool}: {clip(text, 300)}" for tool, ok, text in calls if not ok]
    if failed:
        findings.append(
            Finding(
                "warn",
                "jobs",
                f"{name}: {len(failed)} tool call(s) failed",
                when,
                failed[:5],
            )
        )
    if TOOL_MARKUP.search(last):
        findings.append(
            Finding(
                "warn",
                "jobs",
                f"{name} ended in raw tool-call markup",
                f"{when}. The model stopped mid tool call, so its last step probably never ran.",
                [clip(last)],
            )
        )
    elif not any(ok for _, ok, _ in calls):
        findings.append(
            Finding(
                "warn",
                "jobs",
                f"{name} made no successful tool calls",
                f"{when}. It can't have done its work.",
                [clip(last)] if last else [],
            )
        )
    else:
        cost = (result.get("metrics") or {}).get("totalCost")
        findings.append(
            Finding(
                "info",
                "jobs",
                f"{name} completed",
                f"{when}; {(result.get('duration') or 0) / 1000:.0f} s"
                + (f", ${cost:.4f}" if isinstance(cost, (int, float)) else ""),
                [clip(last)] if last else [],
            )
        )
    return findings


def jobs(env: Env, since: datetime) -> list[Finding]:
    findings = []
    now = env.now()
    for job in env.api_json("/scheduled-jobs").get("jobs", []):
        name = job.get("name", f"job {job.get('id')}")
        runs = env.api_json(f"/scheduled-jobs/{job['id']}/runs").get("runs", [])
        next_run = parse_time(job.get("nextRunAt"))
        if job.get("enabled") and next_run and next_run < now - timedelta(minutes=15):
            # AnythingLLM leaves nextRunAt at the time it just ran (a millisecond before
            # lastRunAt), so a past nextRunAt is only missed if no run started around it.
            started = [parse_time(r.get("startedAt")) for r in runs] + [
                parse_time(job.get("lastRunAt"))
            ]
            if not any(t and t >= next_run - timedelta(minutes=1) for t in started):
                findings.append(
                    Finding(
                        "warn",
                        "jobs",
                        f"{name} missed its run",
                        f"It was due at {next_run:%Y-%m-%d %H:%M} UTC and hasn't run.",
                    )
                )
        for run in runs:
            started = parse_time(run.get("startedAt"))
            if started and started >= since:
                findings.extend(run_findings(name, run, started))
    return findings


# --- deep-research runs ------------------------------------------------------------


def research_running(env: Env) -> list[dict]:
    """Runs with a marker in deep-research/running/ (research-runner writes the log line
    only when a run ends): "interrupted" once the marker has been quiet for its stale_ms (the
    runner restarted under it; the runner moves it into the log when it starts again),
    otherwise "running". As research/runlog.py decides it."""
    runs = []
    for file in (env.runlogs / "deep-research" / "running").glob("*.json"):
        try:
            run = json.loads(file.read_text(encoding="utf-8"))
            quiet = env.now() - datetime.fromtimestamp(file.stat().st_mtime, UTC)
        except (OSError, ValueError):
            continue
        dead = quiet >= timedelta(
            milliseconds=run.get("stale_ms", 3 * 60_000)
        )  # research.runlog.STALE_MS
        runs.append({**run, "status": "interrupted" if dead else "running"})
    return runs


def research_runs_since(env: Env, since: datetime) -> list[dict]:
    """Deep-research runs since `since`, newest first: the run log's lines, and the runs
    still running or interrupted (research_running).

    Logs are one file per month (YYYY-MM.jsonl), so older months are skipped unread.
    """
    folder = env.runlogs / "deep-research"
    first_month = since.astimezone(UTC).strftime("%Y-%m")
    runs = [
        r
        for r in research_running(env)
        if (parse_time(r.get("started")) or since) >= since
    ]
    for file in folder.glob("*.jsonl") if folder.is_dir() else []:
        if file.stem < first_month:
            continue
        for line in file.read_text(encoding="utf-8").splitlines():
            try:
                run = json.loads(line)
            except json.JSONDecodeError:
                continue
            started = parse_time(run.get("started"))
            if started and started >= since:
                runs.append(run)
    return sorted(runs, key=lambda r: r.get("started", ""), reverse=True)


# A run that didn't end with a report: (severity, title, evidence). A failed run's evidence
# is its error.
ENDED = {
    "failed": ("fail", "Deep research run failed", []),
    "running": ("info", "Deep research run in progress", []),
    "interrupted": (
        "warn",
        "Deep research run was interrupted",
        [
            "it stopped without a result, most likely because AnythingLLM restarted under it"
        ],
    ),
    # Before runs survived a closed chat, closing it stopped them.
    "stopped": ("info", "Deep research run was stopped when its chat closed", []),
}


def research_runs(env: Env, since: datetime) -> list[Finding]:
    findings = []
    for run in research_runs_since(env, since):
        what = f'"{clip(run.get("question", ""), 90)}" ({run.get("depth") or "default"}, {run.get("started", "")[:16]})'
        status = run.get("status")
        stats = run.get("stats") or {}
        if status in ENDED:
            severity, title, evidence = ENDED[status]
            evidence = (
                [clip(run.get("error") or "")] if status == "failed" else evidence
            )
            findings.append(Finding(severity, "research", title, what, evidence))
            continue
        problems = []
        if run.get("published") is False:
            findings.append(
                Finding(
                    "fail",
                    "research",
                    "Deep research report saved but not published",
                    what,
                    [clip(run.get("build_error") or "")],
                )
            )
        if stats.get("failed_searches"):
            problems.append(
                f"{stats['failed_searches']} of {stats.get('searches')} searches failed"
            )
        if stats.get("fact_check", "ok") != "ok":
            problems.append(f"fact-check {stats['fact_check']}")
        for w in stats.get("workers_detail") or []:
            if w.get("stopped") == "search-down":
                problems.append(
                    f"worker stopped because search was down: {clip(w.get('goal', ''), 80)}"
                )
            elif not w.get("findings"):
                problems.append(
                    f"worker found nothing ({w.get('stopped')}): {clip(w.get('goal', ''), 80)}"
                )
        if (run.get("seconds") or 0) > 20 * 60:
            problems.append(f"took {run['seconds'] // 60} min")
        if problems:
            findings.append(
                Finding(
                    "warn",
                    "research",
                    "Deep research run had problems",
                    what,
                    problems[:8],
                )
            )
        else:
            evidence = [
                f"{stats.get('sources')} sources, {run.get('seconds')} s, {run.get('url')}"
            ]
            if run.get("chat_closed"):
                evidence.append(
                    "finished after its chat closed, so the reply never reached the chat"
                )
            findings.append(
                Finding(
                    "info", "research", "Deep research run completed", what, evidence
                )
            )
    return findings


# --- OpenRouter credit ---------------------------------------------------------------


def credit(env: Env, since: datetime) -> list[Finding]:
    """What's left to spend on OpenRouter, which the chat model runs on. Out of credit,
    chats and jobs fail with 402 "This request requires more credits, or fewer max_tokens"."""
    provider = env.settings.get("LLM_PROVIDER", "")
    key = env.settings.get("OPENROUTER_API_KEY", "")
    if provider != "openrouter":
        return [
            Finding(
                "info",
                "llm",
                "OpenRouter credit not checked",
                f"The chat provider is {provider or 'not set'}, not OpenRouter.",
            )
        ]
    if not key:
        return [
            Finding(
                "info",
                "llm",
                "OpenRouter credit not checked",
                "No OPENROUTER_API_KEY is set.",
            )
        ]
    auth = {"Authorization": f"Bearer {key}"}
    status, body = env.http(f"{OPENROUTER}/key", auth)
    if status != 200:
        return [
            Finding(
                "warn",
                "llm",
                f"OpenRouter's key check answered {status or 'nothing'}",
                clip(body.decode(errors="replace").replace(key, "<key>")),
            )
        ]
    data = json.loads(body).get("data") or {}
    left = []  # (whose balance, dollars left)
    if isinstance(data.get("limit_remaining"), (int, float)):
        left.append(("the key's limit", data["limit_remaining"]))
    # The account's balance: a key without a limit of its own can spend all of it.
    status, body = env.http(f"{OPENROUTER}/credits", auth)
    if status == 200:
        c = json.loads(body).get("data") or {}
        if isinstance(c.get("total_credits"), (int, float)) and isinstance(
            c.get("total_usage"), (int, float)
        ):
            left.append(("the account", c["total_credits"] - c["total_usage"]))
    if not left:
        return [
            Finding(
                "info",
                "llm",
                "OpenRouter credit unknown",
                f"Neither the key nor the account reported a balance; the key has used ${data.get('usage') or 0:.2f}.",
            )
        ]
    where, dollars = min(left, key=lambda x: x[1])
    detail = f"${dollars:.2f} left on {where}."
    if dollars < env.credit_fail_usd:
        return [
            Finding(
                "fail",
                "llm",
                "OpenRouter credit is nearly gone",
                f"{detail} Under ${env.credit_fail_usd:.2f}, chats and jobs fail with 402 "
                '"requires more credits". Add credit at https://openrouter.ai/settings/credits.',
            )
        ]
    if dollars < env.credit_warn_usd:
        return [
            Finding(
                "warn",
                "llm",
                "OpenRouter credit is low",
                f"{detail} Under ${env.credit_warn_usd:.2f}, a long reply (up to 131072 tokens) may "
                "not be affordable and fails with 402. Add credit at https://openrouter.ai/settings/credits.",
            )
        ]
    return [
        Finding("info", "llm", f"OpenRouter credit: ${dollars:.2f} left", f"on {where}")
    ]


# --- sites -------------------------------------------------------------------------


def walk(value, path: list[str]) -> tuple[int, int]:
    """(present, missing) for a path like ["sections[]", "stories[]", "url"]."""
    if not path:
        return 1, 0
    key, rest = path[0], path[1:]
    many = key.endswith("[]")
    key = key.removesuffix("[]")
    if not isinstance(value, dict) or key not in value or value[key] in (None, ""):
        return 0, 1
    child = value[key]
    if not many:
        return walk(child, rest)
    present = missing = 0
    for item in child if isinstance(child, list) else []:
        if not rest:
            present += 1
            continue
        p, m = walk(item, rest)
        present, missing = present + p, missing + m
    return present, missing


def sites(env: Env, since: datetime) -> list[Finding]:
    today = env.today()  # entries are dated by Stockholm time
    store = SiteStore(env.sites_source, env.sites_content)
    findings = []
    for site in store.sites():
        everything = store.entries(site.name)
        newest = {}  # each section's newest entry; entries come newest first
        for e in everything:
            newest.setdefault(e.section, e)
        broken = []
        for url in [site.url] + [e.url for e in newest.values()]:
            status, _ = env.http(url)
            if status != 200:
                broken.append(f"{url} → {status or 'no answer'}")
        if broken:
            findings.append(
                Finding(
                    "fail",
                    "sites",
                    f"{site.name}: {len(broken)} page(s) don't load",
                    "Published pages should answer 200.",
                    broken,
                )
            )
        for section in site.sections:
            rules = site.section_extra[section].get("audit", {})
            if rules:
                recent = [e for e in everything if e.section == section][:10]
                findings += _section_rules(
                    store, site.name, section, recent, rules, today
                )
        if env.sites_output is not None:
            findings += _stale_build(
                store.content / site.name, env.sites_output / site.name, site.name
            )
    return findings


def _stale_build(content: Path, output: Path, site: str) -> list[Finding]:
    """A site whose newest entry file is newer than its last build: a write that saved
    but didn't rebuild, so the published site doesn't show it."""
    files = [f for f in content.glob("*/*.md") if not f.name.startswith(("_", "."))]
    if not files:
        return []
    newest = max(files, key=lambda f: f.stat().st_mtime)
    changed = datetime.fromtimestamp(newest.stat().st_mtime, UTC)
    marker = output / MARKER
    if not marker.is_file():
        return [
            Finding(
                "fail",
                "sites",
                f"{site}: site was never built",
                f"{output} has no {MARKER}, so its entries aren't published.",
                [
                    f"newest entry {newest.parent.name}/{newest.stem} saved {changed:%Y-%m-%d %H:%M} UTC"
                ],
            )
        ]
    built = datetime.fromtimestamp(marker.stat().st_mtime, UTC)
    if changed <= built:
        return []
    return [
        Finding(
            "fail",
            "sites",
            f"{site}: entries changed since the last build",
            f"Last built {built:%Y-%m-%d %H:%M} UTC; a later write saved but didn't rebuild, "
            "so the published site is out of date. Any write to the site or `make deploy` rebuilds it.",
            [f"{newest.parent.name}/{newest.stem} saved {changed:%Y-%m-%d %H:%M} UTC"],
        )
    ]


def _section_rules(
    store: SiteStore,
    site: str,
    section: str,
    entries: list[Entry],
    rules: dict,
    today: date,
) -> list[Finding]:
    """A section's [extra.audit]: `max_age_days` for its newest entry, `required` fields."""
    findings = []
    where = f"Required by [extra.audit] in {site}'s content/{section}/_index.md."
    max_age = rules.get("max_age_days")
    if max_age is not None:
        newest = next((e.date for e in entries if e.date), "")
        try:
            age = (today - date.fromisoformat(newest)).days
        except ValueError:
            age = None
        if age is None or age > max_age:
            findings.append(
                Finding(
                    "warn",
                    "sites",
                    f"{site}/{section}: no new entry",
                    f"Newest entry is {newest or 'missing'}; expected one within {max_age} day(s).",
                )
            )
    required = rules.get("required", [])
    extras = []  # each entry read once, whatever the number of rules
    for e in entries if required else []:
        try:
            extras.append((e, store.get(site, section, e.slug)[1]))
        except SiteError:
            continue
    for rule in required:
        short = []
        for e, extra in extras:
            present, missing = walk(extra, rule.split("."))
            if missing:
                short.append(
                    f"{e.section}/{e.slug}: {missing} of {present + missing} missing"
                )
        if short:
            findings.append(
                Finding(
                    "warn",
                    "sites",
                    f"{site}/{section}: entries missing `{rule}`",
                    where,
                    short,
                )
            )
    return findings


# --- everything ----------------------------------------------------------------------

CHECKS: dict[str, Callable[[Env, datetime], list[Finding]]] = {
    "logs": journal,
    "search": searxng,
    "services": runners,
    "llm": credit,
    "jobs": jobs,
    "research": research_runs,
    "sites": sites,
}


def run_all(env: Env, since: datetime) -> list[Finding]:
    """Every check; one that crashes becomes a finding instead of hiding the others."""
    findings = []
    for name, check in CHECKS.items():
        try:
            findings.extend(check(env, since))
        except Exception as e:  # noqa: BLE001 - report, don't stop the audit
            findings.append(
                Finding(
                    "warn",
                    name,
                    f"The {name} check itself failed",
                    clip(f"{type(e).__name__}: {e}", 500),
                )
            )
    return sorted(findings, key=lambda f: SEVERITIES.index(f.severity))


def reported(findings: list[Finding]) -> tuple[list[Finding], int]:
    """The findings a report keeps, in order: every fail and warn, and up to MAX_INFO info
    ones taken an area at a time, and within an area a title at a time, so one job's many
    completed runs don't crowd out other jobs, the search engines or the credit. Also how
    many info findings were left out."""
    info = [f for f in findings if f.severity == "info"]
    repeats: Counter[tuple[str, str]] = Counter()
    by_area: dict[
        str, list[tuple[int, int, Finding]]
    ] = {}  # (title's nth time, position, finding)
    for i, f in enumerate(info):
        repeats[f.area, f.title] += 1
        by_area.setdefault(f.area, []).append((repeats[f.area, f.title], i, f))
    queues = {
        area: deque(f for *_, f in sorted(q, key=lambda x: x[:2]))
        for area, q in by_area.items()
    }
    picked: set[int] = set()
    while len(picked) < min(MAX_INFO, len(info)):
        for q in queues.values():
            if q and len(picked) < MAX_INFO:
                picked.add(id(q.popleft()))
    kept = [f for f in findings if f.severity != "info" or id(f) in picked]
    return kept, len(info) - len(picked)


def to_markdown(findings: list[Finding], since: datetime, left_out: int = 0) -> str:
    """Findings numbered for publish_report's suggestions: fail and warn in full, info in a line."""
    counts = Counter(f.severity for f in findings)
    lines = [
        f"# Audit since {since:%Y-%m-%d %H:%M} UTC: "
        + ", ".join(f"{counts[s]} {s}" for s in SEVERITIES)
        + (f" (and {left_out} more info, left out of the report)" if left_out else "")
    ]
    for n, f in enumerate(findings, 1):
        if n == 1 or f.severity != findings[n - 2].severity:
            lines.append(f"\n## {f.severity}")
        if f.severity == "info":
            lines.append(
                f"- #{n} [{f.area}] {f.title}"
                + (f" — {clip(f.detail, 160)}" if f.detail else "")
            )
            continue
        lines.append(
            f"- #{n} [{f.area}] {f.title}" + (f" — {f.detail}" if f.detail else "")
        )
        lines.extend(f"  - {clip(e, 500)}" for e in f.evidence)
    return "\n".join(lines)


# --- the report ----------------------------------------------------------------------

REPORT_SITE, REPORT_SECTION = "status", "reports"


def worst(findings: list[Finding]) -> str:
    """A report's status: fail if any finding fails, else warn if any warns, else ok."""
    return next(
        (s for s in ("fail", "warn") if any(f.severity == s for f in findings)), "ok"
    )


def report_extra(
    findings: list[Finding], summary: str, suggestions: dict[int, str], status: str = ""
) -> dict:
    """The status site's fields for a report (see its agent_help); `suggestions` by finding number."""
    return {
        "status": status or worst(findings),
        "summary": " ".join(summary.split()),
        "findings": [
            {
                "severity": f.severity,
                "area": f.area,
                "title": f.title,
                "detail": " — ".join(
                    filter(
                        None, [f.detail, "; ".join(clip(e, 300) for e in f.evidence)]
                    )
                ),
                "suggestion": " ".join(suggestions.get(n, "").split()),
            }
            for n, f in enumerate(findings, 1)
        ],
    }


def publish(
    store: SiteStore,
    day: date,
    findings: list[Finding],
    summary: str,
    suggestions: dict[int, str],
    status: str = "",
) -> Entry:
    """Write (or replace) the day's report on the status site and rebuild it."""
    slug = day.isoformat()
    return store.write(
        REPORT_SITE,
        REPORT_SECTION,
        slug,
        report_title(day, findings),
        slug,
        report_extra(findings, summary, suggestions, status),
        overwrite=True,
    )


def report_title(day: date, findings: list[Finding]) -> str:
    """The report's title says what it found, since the stylesheet can restyle the page's body
    but not the browser tab or the feed, which is where the title shows."""
    fails = sum(f.severity == "fail" for f in findings)
    warns = sum(f.severity == "warn" for f in findings)
    found = ", ".join(
        filter(
            None,
            [
                f"{fails} failing" if fails else "",
                f"{warns} warning{'s' if warns > 1 else ''}" if warns else "",
            ],
        )
    )
    return f"System audit — {day:%B} {day.day}, {day.year} — {found or 'all checks passed'}"


def recent_lines(
    entries: Iterable[dict], service: str, contains: str, limit: int = 100
) -> list[str]:
    """The last `limit` messages from a service containing `contains`, kept in a bounded buffer."""
    needle = contains.lower()
    return list(
        deque(
            (
                e["message"]
                for e in entries
                if e["service"] == service and needle in e["message"].lower()
            ),
            maxlen=limit,
        )
    )
