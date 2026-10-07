"""agents-runner's side of AnythingLLM's scheduled jobs: listing them, deleting or disabling
one, and one-off jobs, which AnythingLLM doesn't have (its cron is UTC, five fields, and
repeats), so they're made here and deleted once they've run. The scheduled-jobs and
remind-once skills ask for these through runner.Runner's ops; both refuse a delegated task.

  list                 every job: its cron (UTC), next and last run in the user's time,
                       the last run's status, and whether it's a one-off, and a missed one
  delete / disable id  shows the job, and acts only with apply. Never a job the repo
                       manages (hostctl.jobs; deploy keeps it), nor one with a run queued or
                       going, since AnythingLLM stops a running run on DELETE and PUT.
  remind_once          a one-off job, "[once] <name>": a prompt, its tools, and a local
                       date-time, as a cron for that minute, day and month in UTC. Without
                       apply it shows what it would make, for the user to agree to.

A one-off made here goes into a registry (data_dir()/agents/once.json: {id, name, fire_at,
state}), and the poller (`sweep`, every POLL seconds from runner.main) looks at those jobs
alone, never one only named "[once] …". Once fire_at + GRACE has passed and the job has no
run queued or going, a completed run started at or after fire_at gets the job deleted; a
job that never ran is `missed`, and one whose runs failed is `failed`: both are disabled
(its cron would run it again in a year), kept so the result stays readable, logged once and
shown by `list`, and the agent offers to delete it.
A manual run before fire_at doesn't count. While the registry is empty, a tick reads the
file and does nothing else.

Config (environment, from host.env through the unit):
  USER_TIMEZONE  the user's time zone, for one-off times and the times `list` shows
                 (an IANA name; default Europe/Stockholm)
"""

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import hostrpc
from hostctl import jobs as hostjobs
from hostrpc import RunnerError

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


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise RunnerError(
            f"USER_TIMEZONE is '{name}', which isn't a time zone (e.g. Europe/Stockholm); "
            "fix it in host.env and restart agents-runner."
        ) from None


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
        raise RunnerError('tools must be a list of tool ids, e.g. ["@@mcp_sites"]')
    found = list(dict.fromkeys(t.strip() for t in tools if t.strip()))
    if len(found) > MAX_TOOLS:
        raise RunnerError(f"give at most {MAX_TOOLS} tools")
    return found


def tools_text(tools: list[str] | None) -> str:
    """A job's tools; it runs with none when it has none (AnythingLLM reads null as [])."""
    return ", ".join(tools) if tools else "none (it answers from its prompt alone)"


def clipped(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


class Registry:
    """The one-offs made here: a JSON list in `path`, replaced whole (hostrpc.atomic_write).
    Its users hold `lock` from reading it to writing it."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()

    def _read(self) -> list[dict]:
        try:
            entries = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            raise RunnerError(f"the one-off registry {self.path} is unreadable: {e}")
        if not isinstance(entries, list):
            raise RunnerError(f"the one-off registry {self.path} isn't a list")
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
    repo_names: Callable[[], set[str]] = field(
        default=lambda: set(hostjobs.repo_jobs())
    )
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    @property
    def tz(self) -> ZoneInfo:
        return zone(self.timezone)

    async def managed(self) -> set[str]:
        return await asyncio.to_thread(self.repo_names)

    # list

    async def listing(self) -> str:
        tz, now = self.tz, self.clock()
        jobs = await self.client.jobs()
        if not jobs:
            return "AnythingLLM has no scheduled jobs."
        once = {e["id"]: e for e in await self.registry.read()}
        managed = await self.managed()
        lines = [
            (
                f"{len(jobs)} scheduled job{'s' * (len(jobs) != 1)} (times in {tz.key}; "
                "a cron is UTC):"
            )
        ]
        for job in sorted(jobs, key=lambda j: j.get("id") or 0):
            lines.append(
                self.line(job, once.get(job.get("id")), job["name"] in managed, tz, now)
            )
        return "\n".join(lines)

    def line(
        self,
        job: dict,
        entry: dict | None,
        managed: bool,
        tz: ZoneInfo,
        now: datetime,
    ) -> str:
        latest = job.get("latestRun") or {}
        last = when(latest.get("startedAt")) or when(job.get("lastRunAt"))
        status = f" ({latest['status']})" if latest.get("status") else ""
        nxt = when(job.get("nextRunAt"))
        if entry:
            fire = when(entry.get("fire_at"))
            what = f"one-off at {local(fire, tz)}"
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
        else:
            what = f'cron "{job.get("schedule")}" (UTC), next {local(nxt, tz)}'
        parts = [
            f'- id {job.get("id")} "{job["name"]}": {what}',
            f"last run {local(last, tz)}{status}",
            "enabled" if job.get("enabled") else "disabled",
            f"tools: {tools_text(job.get('tools'))}",
        ]
        if managed:
            parts.append("managed by the repo, so not deleted or disabled here")
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
        try:
            job_id = int(job_id)
        except (TypeError, ValueError):
            raise RunnerError(
                f"give the id of the job to {action} (scheduled-jobs list shows them)"
            ) from None
        try:
            job = await self.client.job(job_id)
        except NotFound:
            raise RunnerError(
                f"AnythingLLM has no scheduled job {job_id} (scheduled-jobs list shows "
                "them)"
            ) from None
        if job["name"] in await self.managed():
            raise RunnerError(
                f'"{job["name"]}" is managed by the repo (anythingllm/scheduled-jobs), '
                "which deploy keeps in place; it can't be deleted or disabled here. It can "
                "be turned off in AnythingLLM's Scheduled Jobs, which deploy leaves be."
            )
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
        if once:
            timing = f"one-off at {local(when(once.get('fire_at')), tz)}"
        else:
            timing = (
                f'cron "{job.get("schedule")}" (UTC), next '
                f"{local(when(job.get('nextRunAt')), tz)}"
            )
        latest = job.get("latestRun") or {}
        last = when(latest.get("startedAt")) or when(job.get("lastRunAt"))
        status = f" ({latest['status']})" if latest.get("status") else ""
        return (
            f'Job {job["id"]} "{job["name"]}": {timing}; '
            f"{'enabled' if job.get('enabled') else 'disabled'}; last run "
            f"{local(last, tz)}{status}\nTools: {tools_text(job.get('tools'))}\n"
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
        name = CONTROL.sub(" ", str(name or "")).strip()
        name = name.removeprefix(ONCE.strip()).strip()
        if not name:
            raise RunnerError("give the one-off a short name, e.g. 'stretch'")
        if len(name) > MAX_NAME:
            raise RunnerError(f"the name is over {MAX_NAME} characters")
        prompt = str(prompt or "").strip()
        if not prompt:
            raise RunnerError(
                "give the prompt: what the agent is asked to do or say at that time"
            )
        if len(prompt) > MAX_PROMPT:
            raise RunnerError(f"the prompt is over {MAX_PROMPT} characters")
        wanted = tool_list(tools)
        fire = fire_time(at, tz, now)
        full = ONCE + name
        if any(j.get("name") == full for j in await self.client.jobs()):
            raise RunnerError(
                f'there\'s a job named "{full}" already: pick another name, or delete '
                "that one first"
            )
        if wanted:
            available = {t["id"]: t for t in await self.client.available_tools()}
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
            entry = {
                "id": job["id"],
                "name": full,
                "fire_at": fire.isoformat().replace("+00:00", "Z"),
                "created": now.isoformat().replace("+00:00", "Z"),
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

    # the poller

    async def sweep(self) -> int:
        """One look at the registered one-offs: delete those that have run, mark those
        missed or failed. Returns how many are left in the registry."""
        entries = await self.registry.read()
        if not entries:
            return 0
        now = self.clock()
        gone: set[int] = set()
        states: dict[int, str] = {}
        for entry in entries:
            fire = when(entry.get("fire_at"))
            if fire is None or now < fire + GRACE:
                continue
            job_id = entry["id"]
            try:
                try:
                    await self.client.job(job_id)
                except NotFound:
                    log.info("one-off %s was deleted elsewhere", job_id)
                    gone.add(job_id)
                    continue
                runs = await self.client.runs(job_id)
                if any(r.get("status") in ACTIVE for r in runs):
                    continue
                fired = [
                    r
                    for r in runs
                    if (started := when(r.get("startedAt"))) and started >= fire - EARLY
                ]
                if any(r.get("status") == "completed" for r in fired):
                    await self.client.delete(job_id)
                    log.info(
                        'deleted one-off %s "%s": it has run', job_id, entry["name"]
                    )
                    gone.add(job_id)
                    continue
                state = "failed" if fired else "missed"
                if entry.get("state") != state:
                    # Its cron would run it again in a year: off, until the user decides.
                    await self.client.disable(job_id)
            except AnythingLLMError as e:
                log.warning("one-off %s: %s", job_id, e)
                continue
            if entry.get("state") != state:
                log.warning(
                    'one-off %s "%s" %s at %s; disabled, kept, and listed as %s',
                    job_id,
                    entry["name"],
                    "never ran" if state == "missed" else "failed",
                    entry.get("fire_at"),
                    state,
                )
                states[job_id] = state
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
        while True:
            await asyncio.sleep(POLL)
            try:
                await self.sweep()
            except Exception:
                log.exception("the one-off poller failed a round")
