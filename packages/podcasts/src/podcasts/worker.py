"""What the podcasts' two long-running workers share: the queue folder podcasts-runner asks
them through, their schedules and their heartbeats.

  state/queue/sync-<slug>.json  a sync of one feed (add_podcast, refresh_podcasts);
                                sync-_all.json, of every feed (refresh_podcasts with no
                                slug, and the sync worker's own schedule)
  state/queue/transcribe.json   a transcription pass now (podcasts-transcribe, by hand)
  state/queue/running-sync.json the sync the worker has taken and not finished: asked for
                                again when the worker starts, so one cut short by a kill,
                                an OOM or a crash runs again rather than being lost
  state/queue/<worker>.alive    touched every few seconds while the worker runs
  state/queue/<worker>.last     when its schedule last came due, so a slot missed while
                                it wasn't running comes due as it starts (a timer's
                                Persistent=true)

A request is written whole (atomic_write) and taken by deleting it, so asking again for
what is waiting changes nothing. Nothing here runs a sync or a pass: sync.py and
transcripts.py do, and library.py asks.
"""

import json
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from hostrpc import atomic_write

ALL_FEEDS = "_all"  # every feed's sync; no slug has an underscore
SYNC_WORKER = "sync-worker"
TRANSCRIBE_WORKER = "transcribe-worker"
TRANSCRIBE = "transcribe"
RUNNING = "running-sync"  # not sync-*, so never taken for a feed's request
BEAT_SECONDS = 5  # how often a worker touches its heartbeat
STALE_SECONDS = 60  # a heartbeat older than this: the worker isn't running
POLL_SECONDS = 2  # how often an idle worker looks at the queue
TARGET_RE = re.compile(rf"[a-z0-9-]+|{ALL_FEEDS}")


class Queue:
    """state/queue/: the requests waiting for the workers, and their heartbeats."""

    def __init__(self, state: Path | str):
        self.folder = Path(state) / "queue"

    def _file(self, name: str) -> Path:
        return self.folder / f"{name}.json"

    def ask(self, name: str) -> None:
        """Leave the request `name` (sync-<slug>, transcribe) for a worker to take."""
        self.folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        atomic_write(self._file(name), json.dumps({"asked": stamp}) + "\n")

    def take(self, name: str) -> bool:
        """Take the request `name` if it's waiting."""
        try:
            self._file(name).unlink()
        except FileNotFoundError:
            return False
        return True

    def ask_sync(self, target: str) -> None:
        """A sync of the feed `target`, or of every feed (ALL_FEEDS)."""
        if not TARGET_RE.fullmatch(target):
            raise ValueError(f"not a feed's slug: {target!r}")
        self.ask(f"sync-{target}")

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
            self.hold(ALL_FEEDS)
            for target in waiting:
                self.take(f"sync-{target}")
            return ALL_FEEDS
        for target in waiting:
            self.hold(target)
            if self.take(f"sync-{target}"):
                return target
        self.done()
        return None

    def hold(self, target: str) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        atomic_write(self._file(RUNNING), json.dumps({"target": target}) + "\n")

    def held(self) -> str | None:
        """The sync taken and not done, if one is: a worker that stopped mid-sync."""
        try:
            target = json.loads(self._file(RUNNING).read_text()).get("target", "")
        except (OSError, ValueError, AttributeError):
            return None
        return (
            target if isinstance(target, str) and TARGET_RE.fullmatch(target) else None
        )

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
    worker wasn't running. With no file yet, the first is the next slot, as a new timer's."""

    def __init__(self, file: Path, hours: int, minute: int = 0):
        assert 24 % hours == 0, hours
        self.file, self.hours, self.minute = file, hours, minute

    def slot(self, now: datetime) -> datetime:
        """The latest slot at or before `now` (local time, with its offset)."""
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
        if self.slot(now) <= last:
            return False
        self.ran(now)
        return True
