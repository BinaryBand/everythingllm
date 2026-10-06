"""podcasts-sync-worker: runs the podcast syncs, one at a time, as they're asked for and
every 6 hours.

A long-running service (podcasts-sync-worker.service). It looks at the queue every couple
of seconds (worker.Queue): podcasts-runner's start_sync leaves sync-<slug>.json or
sync-_all.json there, and every 6 hours, at 00:00, 06:00, 12:00 and 18:00 local time, the
worker asks for every feed's itself (worker.Every, which also runs a slot missed while it
was down as soon as it starts, as the timer it replaced did). A sync of every feed takes
the single feeds' requests waiting with it; what is asked for during a sync waits for the
next. Each sync is Library.sync under sync.lock, as before: one that finds the lock held (a
transcript being saved, an old sync still finishing) is asked for again, keeping its tries
(below), and said once. Until the lock is free, which can take hours, the worker takes
nothing and only looks at the lock, with a plain flock rather than a Library, every
BLOCKED_SECONDS. Its output goes to sync.log, as the unit's did, and a crash to
last_sync.json, so list_podcasts can tell; the worker carries on with the next.

SIGTERM stops it between steps: idle, at once; in a sync, at the next feed or scrub, or
partway through a download, after which the sync is asked for again so that the next start
finishes it. A sync is held (worker.Queue.hold) from when it's taken until it's done, so
one the worker didn't live through (killed after its stop timeout, out of memory, a crash)
is asked for again when the worker next starts.

  podcasts-sync [slug]   asks for a sync by hand (every feed without a slug)

Config (environment, from host.env and the unit): PODCASTS_STATE, PODCASTS_DIR,
PODCASTS_BASE_URL, PODCASTS_TZ and ANYTHINGLLM_STORAGE, as podcasts-runner reads them
(tools.py).
"""

import os
import signal
import sys
import threading
import traceback
from collections.abc import Callable
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

import httpx

from podcasts.library import (
    Library,
    LibraryError,
    _now,
    make_client,
    sync_lock_held,
)
from podcasts.worker import (
    ALL_FEEDS,
    POLL_SECONDS,
    RESUMES,
    SYNC_WORKER,
    Every,
    Queue,
    heartbeat,
)

EVERY_HOURS = 6
BLOCKED_SECONDS = 30  # how often a worker waiting for sync.lock looks at it again


@contextmanager
def output_to(file: Path):
    """Send this process's output to `file` meanwhile, appended: Python's (print, tracebacks)
    and what libraries write to its file descriptors themselves (PyAV), as the sync unit's
    StandardOutput=append: did."""
    sys.stdout.flush()
    sys.stderr.flush()
    with open(file, "a", buffering=1) as log:
        saved = os.dup(1), os.dup(2)
        try:
            os.dup2(log.fileno(), 1)
            os.dup2(log.fileno(), 2)
            with redirect_stdout(log), redirect_stderr(log):
                yield
        finally:
            os.dup2(saved[0], 1)
            os.dup2(saved[1], 2)
            os.close(saved[0])
            os.close(saved[1])


class SyncWorker:
    def __init__(
        self,
        state: Path,
        library: Callable[[], Library] = Library.from_env,
        client: Callable[[], httpx.Client] = make_client,
        log: Callable[[str], None] = lambda msg: print(msg, flush=True),
    ):
        """`library` gives the library for each sync, so one sees the settings of the
        moment (the model's key); `client` the HTTP client for each."""
        self.state = Path(state)
        self.queue = Queue(state)
        self.every = Every(self.queue.folder / f"{SYNC_WORKER}.last", EVERY_HOURS)
        self.library, self.client, self.log = library, client, log
        self.stop = threading.Event()
        self.blocked = False  # the last sync found sync.lock held

    def step(self) -> bool:
        """Ask for the scheduled sync if it's due, and run the next sync asked for; False
        when there was none to run (or it has to wait), so the worker waits a moment."""
        if self.every.due():
            self.queue.ask_sync(ALL_FEEDS)
        if self.blocked:
            if sync_lock_held(self.state):
                return False  # still held: said already, and nothing taken meanwhile
            self.blocked = False
        target = self.queue.take_sync()
        if target is None:
            return False
        _, tries = self.queue.held() or (target, 0)
        ran = self.sync(target)
        if not ran:  # it didn't run, so the times it died with a worker still count
            self.queue.ask_sync(target, tries)
        elif self.stop.is_set():  # for the next start
            self.queue.ask_sync(target)
        self.queue.done()
        return ran

    def resume(self) -> None:
        """Ask again for the sync a worker before this one took and didn't finish, unless
        it has died with the worker RESUMES times in a row (one that runs it out of
        memory every time): that one waits for the schedule."""
        if (held := self.queue.held()) is not None:
            target, tries = held
            if tries < RESUMES:
                self.queue.ask_sync(target, tries + 1)
                self.log(f"asked again for the sync of {target}, which didn't finish")
            else:
                self.log(
                    f"the sync of {target} didn't finish {tries + 1} times in a row; "
                    "it waits for the next scheduled sync"
                )
        self.queue.done()

    def sync(self, target: str) -> bool:
        """Run a sync of `target`; False if another held sync.lock."""
        what = "every feed" if target == ALL_FEEDS else target
        lib = self.library()
        lib.stopping = self.stop.is_set
        self.log(f"syncing {what}")
        with output_to(lib.state / "sync.log"):
            print(f"{_now()} syncing {what}", flush=True)
            try:
                with self.client() as client:
                    ran = lib.sync(client, "" if target == ALL_FEEDS else target)
            except Exception:  # noqa: BLE001 - recorded; the worker goes on to the next
                tb = traceback.format_exc()
                print(tb, end="", flush=True)
                lib.sync_crashed(tb)
                ran = True
            else:
                if not ran:
                    print("another sync is running; asked for again", flush=True)
        stopped = " (stopped part-way)" if self.stop.is_set() else ""
        self.log(f"synced {what}{stopped}" if ran else "sync.lock is held; waiting")
        self.blocked = not ran
        return ran

    def run(self) -> None:
        """Until `stop` is set."""
        self.resume()
        with heartbeat(self.queue, SYNC_WORKER):
            while not self.stop.is_set():
                if not self.step():
                    self.stop.wait(BLOCKED_SECONDS if self.blocked else POLL_SECONDS)


def main() -> None:
    state = Library.from_env().state  # a bad config shows in the journal now
    worker = SyncWorker(state)
    signal.signal(signal.SIGTERM, lambda *_: worker.stop.set())
    worker.log(f"waiting for syncs in {worker.queue.folder}")
    worker.run()
    worker.log("stopped")


def ask() -> None:
    """podcasts-sync [slug]: ask the worker for a sync of one feed, or of every feed."""
    slug = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != ALL_FEEDS else ""
    lib = Library.from_env()
    if slug and slug not in lib.feeds():
        sys.exit(f"podcasts-sync: no podcast named '{slug}'")
    try:
        now = lib.start_sync(slug)
    except LibraryError as e:
        sys.exit(f"podcasts-sync: asked, but {e}")
    print(
        f"asked for a sync of {slug or 'every feed'}"
        + ("" if now else "; it starts once the running one finishes")
    )


if __name__ == "__main__":
    main()
