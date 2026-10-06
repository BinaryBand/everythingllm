"""What the podcasts' two long-running workers share: the queue folder podcasts-runner asks
them through, their schedules and their heartbeats.

  state/queue/sync-<slug>.json  a sync of one feed (add_podcast, refresh_podcasts);
                                sync-_all.json, of every feed (refresh_podcasts with no
                                slug, and the sync worker's own schedule)
  state/queue/transcribe.json   a transcription pass now (podcasts-transcribe, by hand)
  state/queue/running-sync.json the sync the worker has taken and not finished: asked for
                                again when the worker starts, so one cut short by a kill,
                                an OOM or a crash runs again rather than being lost (up to
                                RESUMES times in a row, so one that kills the worker every
                                time waits for the schedule instead)
  state/queue/<worker>.alive    touched every few seconds while the worker runs
  state/queue/<worker>.last     when its schedule last came due, so a slot missed while
                                it wasn't running comes due as it starts (a timer's
                                Persistent=true)

Quiet hours (PODCASTS_QUIET_HOURS, e.g. 22:00-06:00, in PODCASTS_TZ; unset, none): the
hours neither worker does its loud work, so the machine's fans stay quiet. A sync still
downloads then, but doesn't read episodes for their ads or cut them; those episodes wait,
unpublished, for the first sync after (06:00's). The transcription worker starts no
episode. Like PODCASTS_TRANSCRIBE_THREADS, it's read from host.env (the repo's, which the
containers mount) each time it's asked, so a change there needs no restart.

A request is written whole (atomic_write) and taken by deleting it, so asking again for
what is waiting changes nothing. Nothing here runs a sync or a pass: sync.py and
transcripts.py do, and library.py asks.
"""

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, tzinfo
from datetime import time as clock_time
from pathlib import Path

from hostrpc import atomic_write, env_values

from podcasts.rules import user_tz

ALL_FEEDS = "_all"  # every feed's sync; no slug has an underscore
SYNC_WORKER = "sync-worker"
TRANSCRIBE_WORKER = "transcribe-worker"
TRANSCRIBE = "transcribe"
RUNNING = "running-sync"  # not sync-*, so never taken for a feed's request
RESUMES = 2  # times in a row a sync that died with the worker is asked for again
BEAT_SECONDS = 5  # how often a worker touches its heartbeat
STALE_SECONDS = 60  # a heartbeat older than this: the worker isn't running
POLL_SECONDS = 2  # how often an idle worker looks at the queue
TARGET_RE = re.compile(rf"[a-z0-9-]+|{ALL_FEEDS}")
HOST_ENV = Path(__file__).resolve().parents[4] / "host.env"  # the repo's
QUIET = "PODCASTS_QUIET_HOURS"


def host_setting(name: str) -> str:
    """`name` as host.env has it now, else as the worker started with."""
    found = env_values(HOST_ENV, [name], environ=False)
    return found[name] if name in found else os.environ.get(name, "")


class QuietHours:
    """PODCASTS_QUIET_HOURS: `HH:MM-HH:MM`, from the first time until the second, past
    midnight when the second is earlier (22:00-06:00); empty for none."""

    def __init__(self, text: str):
        self.span = None
        if not (text := text.strip()):
            return
        try:
            start, _, end = text.partition("-")
            self.span = (
                clock_time.fromisoformat(start.strip().zfill(5)),
                clock_time.fromisoformat(end.strip().zfill(5)),
            )
        except ValueError:
            raise ValueError(
                f"{QUIET} should be like 22:00-06:00, not {text!r}"
            ) from None

    def __contains__(self, now: clock_time) -> bool:
        if self.span is None:
            return False
        start, end = self.span
        return start <= now < end if start <= end else now >= start or now < end


def quiet_now(log=lambda msg: print(msg, flush=True)) -> bool:
    """Whether it's quiet hours now, as host.env has them; a setting that can't be read
    is said, and counts as none."""
    try:
        hours = QuietHours(host_setting(QUIET))
    except ValueError as e:
        log(f"{e}; no quiet hours until it's fixed")
        return False
    return datetime.now(user_tz()).time().replace(tzinfo=None) in hours


class Queue:
    """state/queue/: the requests waiting for the workers, and their heartbeats."""

    def __init__(self, state: Path | str):
        self.folder = Path(state) / "queue"

    def _file(self, name: str) -> Path:
        return self.folder / f"{name}.json"

    def ask(self, name: str, tries: int = 0) -> None:
        """Leave the request `name` (sync-<slug>, transcribe) for a worker to take;
        `tries`, how many times in a row it died with the worker before."""
        self.folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        asked = {"asked": stamp, **({"tries": tries} if tries else {})}
        atomic_write(self._file(name), json.dumps(asked) + "\n")

    def tries(self, name: str) -> int:
        """The request `name`'s `tries`, 0 for none (or a request that's gone)."""
        try:
            tries = json.loads(self._file(name).read_text()).get("tries", 0)
        except (OSError, ValueError, AttributeError):
            return 0
        return tries if isinstance(tries, int) else 0

    def take(self, name: str) -> bool:
        """Take the request `name` if it's waiting."""
        try:
            self._file(name).unlink()
        except FileNotFoundError:
            return False
        return True

    def ask_sync(self, target: str, tries: int = 0) -> None:
        """A sync of the feed `target`, or of every feed (ALL_FEEDS)."""
        if not TARGET_RE.fullmatch(target):
            raise ValueError(f"not a feed's slug: {target!r}")
        self.ask(f"sync-{target}", tries)

    def syncs(self) -> list[str]:
        """The syncs waiting, the longest-waiting first."""
        found = []
        for f in self.folder.glob("sync-*.json"):
            try:
                found.append((f.stat().st_mtime, f.name[5:-5]))
            except FileNotFoundError:  # taken meanwhile
                continue
        return [t for _, t in sorted(found) if TARGET_RE.fullmatch(t)]

    def take_sync(self) -> str | None:
        """The next sync to run, taken: every feed's if it was asked for, which takes the
        ones of single feeds waiting too, since it syncs them; else the longest-waiting
        feed's. None when none waits. What is asked for after this waits for the next.
        It's held (running-sync.json) before its request goes, until `done`."""
        waiting = self.syncs()
        if ALL_FEEDS in waiting:
            self.hold(ALL_FEEDS, self.tries(f"sync-{ALL_FEEDS}"))
            for target in waiting:
                self.take(f"sync-{target}")
            return ALL_FEEDS
        for target in waiting:
            self.hold(target, self.tries(f"sync-{target}"))
            if self.take(f"sync-{target}"):
                return target
        self.done()
        return None

    def hold(self, target: str, tries: int = 0) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        held = {"target": target, "tries": tries}
        atomic_write(self._file(RUNNING), json.dumps(held) + "\n")

    def held(self) -> tuple[str, int] | None:
        """The sync taken and not done, and how many times in a row it had died before,
        if one is: a worker that stopped mid-sync."""
        try:
            held = json.loads(self._file(RUNNING).read_text())
            target, tries = held.get("target", ""), held.get("tries", 0)
        except (OSError, ValueError, AttributeError):
            return None
        if not (isinstance(target, str) and TARGET_RE.fullmatch(target)):
            return None
        return target, tries if isinstance(tries, int) else 0

    def done(self) -> None:
        """The held sync finished, or was asked for again."""
        self._file(RUNNING).unlink(missing_ok=True)

    def _alive(self, worker: str) -> Path:
        return self.folder / f"{worker}.alive"

    def beat(self, worker: str) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._alive(worker).touch()

    def alive(self, worker: str, stale: float = STALE_SECONDS) -> bool:
        """Whether `worker` touched its heartbeat in the last `stale` seconds."""
        try:
            return time.time() - self._alive(worker).stat().st_mtime < stale
        except FileNotFoundError:
            return False


@contextmanager
def heartbeat(queue: Queue, worker: str, every: float = BEAT_SECONDS):
    """Touch `worker`'s heartbeat every `every` seconds from a thread of its own, so a sync
    or a transcription that takes hours doesn't make the worker look dead. It isn't removed
    at the end: a restart then goes unnoticed, and a worker that stays down shows within
    STALE_SECONDS."""
    stop = threading.Event()

    def beat() -> None:
        while True:
            try:
                queue.beat(worker)
            except OSError as e:  # a full disk, say: say so, and keep trying
                print(f"{worker}: couldn't touch its heartbeat: {e}", flush=True)
            if stop.wait(every):
                return

    thread = threading.Thread(target=beat, name=f"{worker} heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


class Every:
    """A systemd timer's OnCalendar=00/<hours>:<minute> with Persistent=true, kept in
    `file`: due once in each slot (local time), and at once when a slot passed while the
    worker wasn't running. With no file yet, the first is the next slot, as a new timer's.

    Slots are compared on the wall clock, in `tz` (default this machine's zone), not as
    instants: when the clocks go back an hour, the slot that already ran is still the
    latest one, rather than coming an hour later in UTC and running twice."""

    def __init__(
        self, file: Path, hours: int, minute: int = 0, tz: tzinfo | None = None
    ):
        assert 24 % hours == 0, hours
        self.file, self.hours, self.minute, self.tz = file, hours, minute, tz

    def wall(self, at: datetime) -> datetime:
        """`at` on the wall clock: its local date and time, without an offset."""
        return at.astimezone(self.tz).replace(tzinfo=None)

    def slot(self, now: datetime) -> datetime:
        """The latest slot at or before `now`, on the wall clock."""
        now = self.wall(now)
        at = now.replace(
            hour=now.hour - now.hour % self.hours,
            minute=self.minute,
            second=0,
            microsecond=0,
        )
        return at if at <= now else at - timedelta(hours=self.hours)

    def last(self) -> datetime | None:
        try:
            text = self.file.read_text().strip()
            # Local time, as the slots are, whatever offset it was written with.
            return datetime.fromisoformat(text).astimezone()
        except (OSError, ValueError):
            return None

    def ran(self, now: datetime) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.file, now.astimezone().isoformat(timespec="seconds") + "\n")

    def due(self, now: datetime | None = None) -> bool:
        """Whether a slot came since it last ran; noted as run when it did."""
        now = now or datetime.now().astimezone()
        last = self.last()
        if last is None:  # first start (or a file that can't be read): wait for a slot
            self.ran(now)
            return False
        if self.slot(now) <= self.wall(last):
            return False
        self.ran(now)
        return True
