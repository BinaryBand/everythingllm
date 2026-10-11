"""The ingest queue: files whose text `ingest` read, embedded one at a time in a thread of
the runner's own and then kept in the store, since the model embeds about ten chunks a
second on this machine and a call answers within 45 s.

A file goes in the store whole, once all its chunks are embedded, replacing what an
earlier ingest kept of it; until then recall doesn't find it. What's queued is in memory
only: a restart drops it, and `list` shows which files the store has, so the agent can
ingest the rest again.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass

from memory.store import Embed, Store

log = logging.getLogger("memory-runner")

MAX_PENDING_CHUNKS = 6000  # per workspace: about ten minutes of embedding
CHUNKS_PER_SECOND = 10  # for the estimate the reply gives


@dataclass
class Job:
    workspace: str
    path: str
    pieces: list[str]


class Ingester:
    """Embeds queued files with `embed` once `ready` returns (it raises while the model
    can't be had) and keeps them in `store`."""

    def __init__(
        self,
        store: Store,
        embed: Embed,
        ready: Callable[[], None],
    ):
        self.store = store
        self.embed = embed
        self.ready = ready
        self.jobs: queue.Queue[Job] = queue.Queue()
        self.lock = threading.Lock()
        self.pending: dict[str, dict[str, int]] = {}  # workspace -> path -> chunks
        self.failed: dict[str, dict[str, str]] = {}  # workspace -> path -> why

    def start(self) -> None:
        threading.Thread(target=self.work, name="ingest", daemon=True).start()

    def room(self, workspace: str) -> int:
        """Chunks the workspace may still queue."""
        with self.lock:
            return MAX_PENDING_CHUNKS - sum(self.pending.get(workspace, {}).values())

    def add(self, workspace: str, path: str, pieces: list[str]) -> None:
        with self.lock:
            waiting = self.pending.setdefault(workspace, {})
            waiting[path] = waiting.get(path, 0) + len(pieces)
            self.failed.get(workspace, {}).pop(path, None)
        self.jobs.put(Job(workspace, path, pieces))

    def waiting(self, workspace: str) -> dict[str, int]:
        with self.lock:
            return dict(self.pending.get(workspace, {}))

    def failures(self, workspace: str) -> dict[str, str]:
        with self.lock:
            return dict(self.failed.get(workspace, {}))

    def work(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                self.one(job)
            except Exception as e:
                log.exception("ingesting %s for %s failed", job.path, job.workspace)
                with self.lock:
                    self.failed.setdefault(job.workspace, {})[job.path] = str(e)
            finally:
                with self.lock:
                    waiting = self.pending.get(job.workspace, {})
                    left = waiting.get(job.path, 0) - len(job.pieces)
                    if left > 0:
                        waiting[job.path] = left
                    else:
                        waiting.pop(job.path, None)
                self.jobs.task_done()

    def one(self, job: Job) -> None:
        self.ready()
        self.store.replace_file(
            job.workspace, job.path, job.pieces, self.embed(job.pieces)
        )
        log.info(
            "ingested %s for %s (%d chunks)", job.path, job.workspace, len(job.pieces)
        )
