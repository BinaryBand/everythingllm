"""podcasts-sync-worker: runs the podcast syncs, one at a time, as they're asked for and
every 6 hours.

A long-running service (podcasts-sync-worker.service). It looks at the queue every couple
of seconds (worker.Queue): podcasts-runner's start_sync leaves sync-<slug>.json or
sync-_all.json there, and every 6 hours, at 00:00, 06:00, 12:00 and 18:00 local time, the
worker asks for every feed's itself (worker.Every, which also runs a slot missed while it
was down as soon as it starts, as the timer it replaced did). A sync of every feed takes
the single feeds' requests waiting with it; what is asked for during a sync waits for the
next. Each sync is Library.sync under sync.lock, as before: one that finds the lock held (a
transcript being saved) is asked for again and tried after the next look. Its output goes
to sync.log, as the unit's did, and a crash to last_sync.json, so list_podcasts can tell;
the worker carries on with the next.

SIGTERM stops it between steps: idle, at once; in a sync, at the next feed, download or
scrub, after which the sync is asked for again so that the next start finishes it.

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

from podcasts.library import Library, LibraryError, _now, make_client
from podcasts.worker import (
    ALL_FEEDS,
    POLL_SECONDS,
    SYNC_WORKER,
    Every,
    Queue,
    heartbeat,
)

EVERY_HOURS = 6


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
        self.queue = Queue(state)
        self.every = Every(self.queue.folder / f"{SYNC_WORKER}.last", EVERY_HOURS)
        self.library, self.client, self.log = library, client, log
        self.stop = threading.Event()

    def step(self) -> bool:
        """Ask for the scheduled sync if it's due, and run the next sync asked for; False
        when there was none to run (or it has to wait), so the worker waits a moment."""
        if self.every.due():
            self.queue.ask_sync(ALL_FEEDS)
        target = self.queue.take_sync()
        if target is None:
            return False
        ran = self.sync(target)
        if not ran or self.stop.is_set():
            self.queue.ask_sync(target)  # for the next look, or the next start
        return ran

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
        return ran

    def run(self) -> None:
        """Until `stop` is set."""
        with heartbeat(self.queue, SYNC_WORKER):
            while not self.stop.is_set():
                if not self.step():
                    self.stop.wait(POLL_SECONDS)


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
