"""agents-runner's side of AnythingLLM's scheduled jobs: listing them, deleting or disabling
one (shown first, done only with apply), making a recurring one from a UTC cron (shown
first, made only with apply), and one-off jobs, which AnythingLLM doesn't have:
made here as "[once] <name>" with a cron for that minute, day and month in UTC, kept in a
registry (data_dir()/agents/once.json), and deleted by the poller once they've run, or
disabled if they missed or failed. The scheduled-jobs, schedule-job and remind-once skills
ask for these through runner.Runner's ops, which refuse a delegated task and a scheduled
job. The README's "Scheduled jobs from a chat" has the rules.

A job runs with every tool approved, so only a chat may make one. AnythingLLM's own
create-scheduled-job is turned off (the setup checklist checks), and while a delegation
runs, `guarding` watches for a job made any other way (a built-in tool turned on again)
and disables it.

Config (environment, from host.env through the unit):
  USER_TIMEZONE  the user's time zone, for one-off times and the times `list` shows
                 (an IANA name; default Europe/Stockholm)
"""

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import hostrpc
from hostrpc import RunnerError
from runs.runlog import iso

from agents.anythingllm import AnythingLLMError, InternalAPI, NotFound

log = logging.getLogger("agents-runner")

DEFAULT_TIMEZONE = "Europe/Stockholm"
ONCE = "[once] "  # a one-off's name starts with it
GRACE = timedelta(minutes=2)  # after fire_at, before the poller looks
EARLY = timedelta(seconds=30)  # a run started this much before fire_at still counts
LEAD = timedelta(minutes=1)  # the least time ahead a one-off may be set
AHEAD = timedelta(days=364)  # the most: its cron repeats every year
POLL = 60  # seconds between the poller's looks
ACTIVE = {"queued", "running"}
MAX_NAME = 80
MAX_PROMPT = 8000
MAX_TOOLS = 40
SHOWN_PROMPT = 2000  # characters of a job's prompt a preview shows
LISTED_PROMPT = 120  # and the list
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# A cron of five fields (minute, hour, day, month, weekday), as AnythingLLM takes it.
CRON_RE = re.compile(r"^[0-9A-Za-z*/,-]+( [0-9A-Za-z*/,-]+){4}$")
WATCH = 10  # seconds between looks for a job made while a delegation runs


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise RunnerError(
            f"USER_TIMEZONE is '{name}', which isn't a time zone (e.g. Europe/Stockholm); "
            "fix it in host.env and restart agents-runner."
        ) from None


def whole_number(value: Any) -> int | None:
    """An id the model gave, as an int: a whole number, or its digits as text; None for
    anything else, since int() would make true 1 and 12.7 12, and act on that one."""
    if isinstance(value, str):
        value = value.strip()
        return int(value) if re.fullmatch(r"[0-9]{1,18}", value) else None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None  # also inf and nan
    return int(value)


def when(text: Any) -> datetime | None:
    """A time AnythingLLM gives ("2026-10-07T12:05:00.000Z"), or None."""
    if not text:
        return None
    try:
        found = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def local(moment: datetime | None, tz: ZoneInfo) -> str:
    """'Wed 2026-10-07 14:05 CEST', or 'never'."""
    if moment is None:
        return "never"
    return moment.astimezone(tz).strftime("%a %Y-%m-%d %H:%M %Z")


def fire_time(at: str, tz: ZoneInfo, now: datetime) -> datetime:
    """When a one-off set for the local date-time `at` runs, in UTC: to the minute, at least
    LEAD and at most AHEAD from now, and only a time that happens exactly once in `tz`."""
    text = str(at or "").strip()
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise RunnerError(
            f"'{text}' isn't a date and time: give the user's local time ({tz.key}) as "
            "YYYY-MM-DD HH:MM, e.g. 2026-10-08 09:30"
        ) from None
    if moment.tzinfo is not None:
        raise RunnerError(
            f"give the user's local time ({tz.key}) without a UTC offset, e.g. "
            f"{moment.strftime('%Y-%m-%d %H:%M')}; it's converted here"
        )
    moment = moment.replace(second=0, microsecond=0)
    first, second = moment.replace(tzinfo=tz), moment.replace(tzinfo=tz, fold=1)
    if first.utcoffset() != second.utcoffset():
        if first.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != moment:
            raise RunnerError(
                f"{moment:%Y-%m-%d %H:%M} doesn't happen in {tz.key}: the clocks go "
                "forward then. Pick a time outside that hour."
            )
        raise RunnerError(
            f"{moment:%Y-%m-%d %H:%M} happens twice in {tz.key}: the clocks go back "
            "then. Pick a time outside that hour."
        )
    fire = first.astimezone(UTC)
    if fire < now + LEAD:
        raise RunnerError(
            f"{local(fire, tz)} has passed, or is less than a minute away; it's "
            f"{local(now, tz)} now. Pick a later time."
        )
    if fire > now + AHEAD:
        raise RunnerError(
            "a one-off can be at most 364 days ahead (its cron repeats every year); for "
            "later, set a nearer reminder to set it."
        )
    return fire


def cron(fire: datetime) -> str:
    """The cron that runs at `fire` (UTC) this year: minute, hour, day and month."""
    return f"{fire.minute} {fire.hour} {fire.day} {fire.month} *"


def tool_list(tools: Any) -> list[str]:
    """The tools a model sends: a list, JSON text of one, or names split by commas."""
    if tools is None or tools == "":
        return []
    if isinstance(tools, str):
        try:
            tools = json.loads(tools)
        except ValueError:
            tools = [t for t in re.split(r"[,\s]+", tools) if t]
    if isinstance(tools, str):
        tools = [tools]
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise RunnerError('tools must be a list of tool ids, e.g. ["@@run-code"]')
    found = list(dict.fromkeys(t.strip() for t in tools if t.strip()))
    if len(found) > MAX_TOOLS:
        raise RunnerError(f"give at most {MAX_TOOLS} tools")
    return found


def job_parts(name: Any, prompt: Any, tools: Any) -> tuple[str, str, list[str]]:
    """A new job's name (control characters made spaces), prompt and tools, checked."""
    name = CONTROL.sub(" ", str(name or "")).strip()
    if not name:
        raise RunnerError("give the job a short name, e.g. 'stretch'")
    if len(name) > MAX_NAME:
        raise RunnerError(f"the name is over {MAX_NAME} characters")
    prompt = str(prompt or "").strip()
    if not prompt:
        raise RunnerError(
            "give the prompt: what the agent is asked to do or say at that time"
        )
    if len(prompt) > MAX_PROMPT:
        raise RunnerError(f"the prompt is over {MAX_PROMPT} characters")
    return name, prompt, tool_list(tools)


def utc_offset(tz: ZoneInfo, now: datetime) -> str:
    """How far `tz` is from UTC at `now`, e.g. "UTC+2"."""
    hours = (now.astimezone(tz).utcoffset() or timedelta()).total_seconds() / 3600
    return f"UTC{hours:+g}" if hours else "UTC"


def tools_text(tools: list[str] | None) -> str:
    """A job's tools; it runs with none when it has none (AnythingLLM reads null as [])."""
    return ", ".join(tools) if tools else "none (it answers from its prompt alone)"


def clipped(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def timing(job: dict, entry: dict | None, tz: ZoneInfo) -> str:
    """When a job runs: a one-off's time, or its cron and next run."""
    if entry:
        return f"one-off at {local(when(entry.get('fire_at')), tz)}"
    nxt = local(when(job.get("nextRunAt")), tz)
    return f'cron "{job.get("schedule")}" (UTC), next {nxt}'


def last_run(job: dict, tz: ZoneInfo) -> str:
    latest = job.get("latestRun") or {}
    last = when(latest.get("startedAt")) or when(job.get("lastRunAt"))
    status = f" ({latest['status']})" if latest.get("status") else ""
    return f"last run {local(last, tz)}{status}"


async def repeat(seconds: float, step: Callable[[], Awaitable[Any]], what: str) -> None:
    """Run `step` every `seconds`, until cancelled; a round that fails is logged."""
    while True:
        await asyncio.sleep(seconds)
        try:
            await step()
        except Exception:
            log.exception("%s failed a round", what)


class Registry:
    """Entries kept across restarts (the one-offs made here, the research runs followed):
    a JSON list in `path`, replaced whole (hostrpc.atomic_write). Its users hold `lock`
    from reading it to writing it."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()

    def _read(self) -> list[dict]:
        try:
            entries = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            raise RunnerError(f"{self.path} is unreadable: {e}")
        if not isinstance(entries, list):
            raise RunnerError(f"{self.path} isn't a list")
        return [e for e in entries if isinstance(e, dict) and "id" in e]

    def _write(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        hostrpc.atomic_write(self.path, json.dumps(entries, indent=2) + "\n", 0o600)

    async def read(self) -> list[dict]:
        return await asyncio.to_thread(self._read)

    async def write(self, entries: list[dict]) -> None:
        await asyncio.to_thread(self._write, entries)


@dataclass
class ScheduledJobs:
    client: InternalAPI
    registry: Registry
    timezone: str = DEFAULT_TIMEZONE
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    made: set[int] = field(default_factory=set)  # the jobs made here
    # While delegations run: each one's note (by run id), the job ids there were when the
    # first started, and the task that watches for new ones.
    watched: dict[str, Callable[[str], None]] = field(default_factory=dict)
    known: set[int] | None = None
    watcher: asyncio.Task | None = None

    @property
    def tz(self) -> ZoneInfo:
        return zone(self.timezone)

    # list

    async def listing(self) -> str:
        tz, now = self.tz, self.clock()
        jobs = await self.client.jobs()
        if not jobs:
            return "AnythingLLM has no scheduled jobs."
        once = {e["id"]: e for e in await self.registry.read()}
        lines = [
            (
                f"{len(jobs)} scheduled job{'s' * (len(jobs) != 1)} (times in {tz.key}; "
                "a cron is UTC):"
            )
        ]
        for job in sorted(jobs, key=lambda j: j.get("id") or 0):
            lines.append(self.line(job, once.get(job.get("id")), tz, now))
        return "\n".join(lines)

    def line(
        self,
        job: dict,
        entry: dict | None,
        tz: ZoneInfo,
        now: datetime,
    ) -> str:
        latest = job.get("latestRun") or {}
        what = timing(job, entry, tz)
        if entry:
            state = self.state(entry, latest, now)
            if state == "missed":
                what += "; MISSED: it never ran, so it was disabled; offer to delete it"
            elif state == "failed":
                what += (
                    f"; its run {latest.get('status') or 'failed'}, so it was disabled; "
                    "offer to delete it"
                )
            elif state == "done":
                what += "; it has run, and is deleted within a few minutes"
        parts = [
            f'- id {job.get("id")} "{job["name"]}": {what}',
            last_run(job, tz),
            "enabled" if job.get("enabled") else "disabled",
            f"tools: {tools_text(job.get('tools'))}",
        ]
        return (
            "; ".join(parts)
            + f"\n  prompt: {clipped(job.get('prompt', ''), LISTED_PROMPT)}"
        )

    @staticmethod
    def state(entry: dict, latest: dict, now: datetime) -> str:
        """pending, done, missed or failed, as far as the job's latest run tells."""
        fire = when(entry.get("fire_at"))
        if fire is None or now < fire + GRACE or latest.get("status") in ACTIVE:
            return "pending"
        started = when(latest.get("startedAt"))
        if started is None or started < fire - EARLY:
            return "missed"
        return "done" if latest.get("status") == "completed" else "failed"

    # delete / disable

    async def act(self, action: str, job_id: Any, apply: bool) -> str:
        if action not in ("delete", "disable"):
            raise RunnerError("action must be list, delete or disable")
        job_id = whole_number(job_id)
        if job_id is None:
            raise RunnerError(
                f"give the id of the job to {action} (scheduled-jobs list shows them)"
            )
        try:
            job = await self.client.job(job_id)
        except NotFound:
            raise RunnerError(
                f"AnythingLLM has no scheduled job {job_id} (scheduled-jobs list shows "
                "them)"
            ) from None
        await self.idle(job)
        tz = self.tz
        once = next(
            (e for e in await self.registry.read() if e.get("id") == job_id), None
        )
        if action == "disable" and not job.get("enabled"):
            return f'Job {job_id} "{job["name"]}" is already disabled.'
        if not apply:
            effect = (
                "Deleting it can't be undone."
                if action == "delete"
                else "Disabling it stops it running until it's turned on again in "
                "AnythingLLM's Scheduled Jobs."
            )
            return (
                f"{self.describe(job, once, tz)}\n\n{effect} Show the user this job, and "
                f"call again with apply true only if they agree to {action} it."
            )
        if action == "delete":
            await self.client.delete(job_id)
            await self.forget(job_id)
            log.info('deleted scheduled job %s "%s"', job_id, job["name"])
            return f'Deleted job {job_id} "{job["name"]}".'
        await self.client.disable(job_id)
        log.info('disabled scheduled job %s "%s"', job_id, job["name"])
        return (
            f'Disabled job {job_id} "{job["name"]}". It stays in AnythingLLM\'s Scheduled '
            "Jobs, where it can be turned on again."
        )

    async def idle(self, job: dict) -> None:
        """Refuse a job with a run queued or going: AnythingLLM would stop it."""
        runs = await self.client.runs(job["id"])
        if any(r.get("status") in ACTIVE for r in runs):
            raise RunnerError(
                f'job {job["id"]} "{job["name"]}" is running right now, and changing it '
                "would stop that run. Try again once it's done."
            )

    def describe(self, job: dict, once: dict | None, tz: ZoneInfo) -> str:
        return (
            f'Job {job["id"]} "{job["name"]}": {timing(job, once, tz)}; '
            f"{'enabled' if job.get('enabled') else 'disabled'}; {last_run(job, tz)}\n"
            f"Tools: {tools_text(job.get('tools'))}\n"
            f"Prompt:\n{clipped(job.get('prompt', ''), SHOWN_PROMPT)}"
        )

    async def forget(self, job_id: int) -> None:
        async with self.registry.lock:
            entries = await self.registry.read()
            if any(e.get("id") == job_id for e in entries):
                await self.registry.write([e for e in entries if e.get("id") != job_id])

    # one-offs

    async def remind_once(
        self, name: str, prompt: str, at: str, tools: Any, apply: bool
    ) -> str:
        tz, now = self.tz, self.clock()
        name, prompt, wanted = job_parts(
            str(name or "").strip().removeprefix(ONCE.strip()), prompt, tools
        )
        fire = fire_time(at, tz, now)
        full = ONCE + name
        await self.can_make(full, wanted)
        schedule = cron(fire)
        timing = (
            f'{local(fire, tz)} ({fire:%Y-%m-%d %H:%M} UTC; cron "{schedule}"), then '
            "deleted"
        )
        if not apply:
            return (
                "A one-off job, not made yet:\n"
                f'Name: "{full}"\nRuns: {timing}\nTools: {tools_text(wanted)}\n'
                f"Prompt (what the agent is asked then, with no chat history; its reply "
                f"is the notification):\n{prompt}\n\n"
                "Show the user all of this, and call again with apply true only once "
                "they agree."
            )
        async with self.registry.lock:
            job = await self.client.create(full, prompt, wanted, schedule)
            self.made.add(job["id"])
            entry = {
                "id": job["id"],
                "name": full,
                "fire_at": iso(fire.timestamp()),
                "state": "pending",
            }
            try:
                await self.registry.write([*await self.registry.read(), entry])
            except (OSError, RunnerError) as e:  # unregistered, it'd run every year
                log.error("couldn't register one-off %s: %s", job["id"], e)
                await self.client.delete(job["id"])
                raise RunnerError(
                    f"couldn't record the one-off, so it wasn't kept: {e}"
                ) from None
        log.info('made one-off %s "%s" for %s', job["id"], full, entry["fire_at"])
        return f'Made one-off job {job["id"]} "{full}": it runs {timing}.'

    async def can_make(self, name: str, wanted: list[str]) -> None:
        """Refuse a name in use and a tool AnythingLLM doesn't have for a job, or hasn't
        set up; looked up all at once."""

        async def none() -> Any:
            return ()

        jobs, tools = await asyncio.gather(
            self.client.jobs(),
            self.client.available_tools() if wanted else none(),
        )
        if any(j.get("name") == name for j in jobs):
            raise RunnerError(
                f'there\'s a job named "{name}" already: pick another name, or delete '
                "that one first"
            )
        available = {t["id"]: t for t in tools}
        unknown = [t for t in wanted if t not in available]
        if unknown:
            some = ", ".join(sorted(available)[:30])
            raise RunnerError(
                f"AnythingLLM has no tool {', '.join(unknown)} for a job. Its tools "
                f"are ids such as: {some}"
            )
        unset = [t for t in wanted if available[t].get("requiresSetup")]
        if unset:
            raise RunnerError(
                f"{', '.join(unset)} need setting up in AnythingLLM before a job can "
                "use them"
            )

    # recurring jobs

    async def schedule(
        self, name: str, prompt: str, schedule: str, tools: Any, apply: bool
    ) -> str:
        name, prompt, wanted = job_parts(name, prompt, tools)
        if name.startswith(ONCE.strip()):
            raise RunnerError(
                f'a name starting with "{ONCE.strip()}" is a one-off\'s: use remind-once '
                "for a job that runs once"
            )
        schedule = " ".join(str(schedule or "").split())
        if not CRON_RE.fullmatch(schedule):
            raise RunnerError(
                "the schedule must be a cron of five fields (minute hour day month "
                'weekday) in UTC, e.g. "0 6 * * 1-5" for 06:00 UTC on weekdays'
            )
        await self.can_make(name, wanted)
        offset = f"{self.tz.key} is {utc_offset(self.tz, self.clock())} now"
        if not apply:
            return (
                "A scheduled job, not made yet:\n"
                f'Name: "{name}"\nRuns: cron "{schedule}", in UTC ({offset})\n'
                f"Tools: {tools_text(wanted)}\n"
                f"Prompt (what the agent is asked each time, with no chat history and "
                f"every tool approved; its reply is the notification):\n{prompt}\n\n"
                "Show the user all of this, with the times in their own time zone, and "
                "call again with apply true only once they agree."
            )
        job = await self.client.create(name, prompt, wanted, schedule)
        self.made.add(job["id"])
        log.info('made scheduled job %s "%s" (cron "%s")', job["id"], name, schedule)
        return (
            f'Made job {job["id"]} "{name}": it runs on cron "{schedule}" (UTC; {offset}). '
            "scheduled-jobs lists, disables or deletes it."
        )

    # while a delegation runs

    @contextlib.asynccontextmanager
    async def guarding(
        self, key: str, note: Callable[[str], None]
    ) -> AsyncIterator[None]:
        """While the delegation `key` runs, disable any scheduled job that appears that
        wasn't made here, telling it through `note`: a delegated task may not make one, and
        only AnythingLLM's own tools, which our skills' refusal doesn't reach, could."""
        # Registered before the first await, so a delegation that starts meanwhile doesn't
        # start a second watcher, which nothing would cancel and which would go on
        # disabling every new job, the user's own too.
        first = not self.watched
        self.watched[key] = note
        try:
            if first:
                self.known = None  # until the listing: a check meanwhile stands in
                self.known = await self.job_ids()
                self.watcher = asyncio.create_task(self.watch())
            yield
        finally:
            del self.watched[key]
            if not self.watched and self.watcher is not None:
                self.watcher.cancel()
                self.watcher = None
            await self.check([note])  # a job made in its last moments

    async def job_ids(self) -> set[int] | None:
        try:
            return {j["id"] for j in await self.client.jobs()}
        except AnythingLLMError as e:
            log.warning(
                "couldn't list the scheduled jobs a delegation starts with: %s", e
            )
            return None

    async def check(self, notes: list[Callable[[str], None]]) -> None:
        """One look for jobs that weren't there when the delegations started."""
        try:
            jobs = await self.client.jobs()
        except AnythingLLMError as e:
            log.warning("couldn't look for jobs made during a delegation: %s", e)
            return
        if self.known is None:  # the first listing failed: this one stands in
            self.known = {j["id"] for j in jobs}
            return
        for job in jobs:
            job_id = job["id"]
            if job_id in self.known or job_id in self.made:
                continue
            self.known.add(job_id)
            try:
                if job.get("enabled"):
                    await self.client.disable(job_id)
            except AnythingLLMError as e:
                log.error(
                    'job %s "%s" appeared during a delegation, and disabling it failed: %s',
                    job_id,
                    job.get("name"),
                    e,
                )
                continue
            log.warning(
                'job %s "%s" appeared during a delegation; disabled it',
                job_id,
                job.get("name"),
            )
            for note in notes:
                note(
                    f'A scheduled job "{job.get("name")}" (id {job_id}) appeared while '
                    "this delegation ran, so it was disabled: a delegated task may not "
                    "make one. If you made it, turn it on again in AnythingLLM's "
                    "Scheduled Jobs."
                )

    async def watch(self) -> None:
        """Check every WATCH seconds, until cancelled."""
        while True:
            await asyncio.sleep(WATCH)
            try:
                await self.check(list(self.watched.values()))
            except Exception:
                log.exception("the delegation's job watch failed a round")

    # the poller

    async def sweep(self) -> int:
        """One look at the registered one-offs that are due, through one job listing and
        the rule `list` shows (state): delete those that have run; disable those that
        missed or failed, since their cron would run them again in a year, and log that
        once. Returns how many are left in the registry."""
        entries = await self.registry.read()
        now = self.clock()
        due = [
            e
            for e in entries
            if (fire := when(e.get("fire_at"))) is not None and now >= fire + GRACE
        ]
        if not due:
            return len(entries)
        jobs = {j.get("id"): j for j in await self.client.jobs()}
        gone: set[int] = set()
        states: dict[int, str] = {}
        for entry in due:
            job_id, job = entry["id"], jobs.get(entry["id"])
            try:
                if job is None:
                    log.info("one-off %s was deleted elsewhere", job_id)
                    gone.add(job_id)
                    continue
                state = self.state(entry, job.get("latestRun") or {}, now)
                if state == "done":
                    await self.client.delete(job_id)
                    log.info('deleted one-off %s "%s": it has run', job_id, job["name"])
                    gone.add(job_id)
                elif state != "pending" and entry.get("state") != state:
                    if job.get("enabled"):
                        await self.client.disable(job_id)
                    log.warning(
                        'one-off %s "%s" %s at %s; disabled, kept, and listed as %s',
                        job_id,
                        job["name"],
                        "never ran" if state == "missed" else "failed",
                        entry.get("fire_at"),
                        state,
                    )
                    states[job_id] = state
            except AnythingLLMError as e:
                log.warning("one-off %s: %s", job_id, e)
        if not gone and not states:
            return len(entries)
        async with self.registry.lock:  # it may have gained entries meanwhile
            entries = [
                {**e, "state": states.get(e["id"], e.get("state"))}
                for e in await self.registry.read()
                if e["id"] not in gone
            ]
            await self.registry.write(entries)
        return len(entries)

    async def poll(self) -> None:
        """Sweep every POLL seconds, until cancelled."""
        await repeat(POLL, self.sweep, "the one-off poller")
