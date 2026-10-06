"""The relay's runs and their events, in SQLite. Each event is written as it arrives, so a
restart loses nothing already received; a terminal event and the run's new status are
written in one transaction, so a run never ends twice.

A run as the API shows it is `public(row)`: the question stays in the store (ntfy needs
its start), out of every response.
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

TERMINAL = {"done": "done", "failed": "failed", "cancelled": "cancelled"}
STATUSES = ("running", "done", "failed", "cancelled")

SCHEMA = """
create table if not exists runs (
  id text primary key,
  client_id text not null unique,
  workspace text not null,
  thread text not null,
  mode text not null,
  question text not null,
  status text not null,
  created_at text not null,
  finished_at text
);
create index if not exists runs_by_thread on runs (workspace, thread, status);
create table if not exists events (
  run_id text not null references runs (id) on delete cascade,
  seq integer not null,
  name text not null,
  data text not null,
  primary key (run_id, seq)
);
"""


def stamp(at: datetime | None = None) -> str:
    return (at or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def public(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "clientId": row["client_id"],
        "workspace": row["workspace"],
        "thread": row["thread"],
        "mode": row["mode"],
        "status": row["status"],
        "createdAt": row["created_at"],
        "finishedAt": row["finished_at"],
    }


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode = wal")
        self.db.execute("pragma foreign_keys = on")
        self.db.executescript(SCHEMA)
        path.chmod(0o600)  # questions and answers

    def close(self) -> None:
        self.db.close()

    def create(
        self,
        run_id: str,
        client_id: str,
        workspace: str,
        thread: str,
        mode: str,
        question: str,
    ) -> sqlite3.Row:
        self.db.execute(
            "insert into runs values (?, ?, ?, ?, ?, ?, 'running', ?, null)",
            (run_id, client_id, workspace, thread, mode, question, stamp()),
        )
        row = self.get(run_id)
        assert row is not None
        return row

    def get(self, run_id: str) -> sqlite3.Row | None:
        return self.db.execute("select * from runs where id = ?", (run_id,)).fetchone()

    def by_client(self, client_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "select * from runs where client_id = ?", (client_id,)
        ).fetchone()

    def running_on(self, workspace: str, thread: str) -> sqlite3.Row | None:
        return self.db.execute(
            "select * from runs where workspace = ? and thread = ? and status = 'running'",
            (workspace, thread),
        ).fetchone()

    def runs(self, status: str | None = None) -> list[sqlite3.Row]:
        """Oldest first; every run when `status` is None."""
        where, args = ("where status = ?", (status,)) if status else ("", ())
        return self.db.execute(
            f"select * from runs {where} order by created_at, rowid", args
        ).fetchall()

    def append(self, run_id: str, name: str, data: dict[str, Any]) -> int:
        """Add the run's next event and return its id; a terminal event also ends the run.
        Returns 0 and adds nothing when the run has already ended."""
        with self.db:
            self.db.execute("begin immediate")
            run = self.get(run_id)
            if run is None or run["status"] != "running":
                return 0
            seq = self.db.execute(
                "select coalesce(max(seq), 0) + 1 from events where run_id = ?",
                (run_id,),
            ).fetchone()[0]
            self.db.execute(
                "insert into events values (?, ?, ?, ?)",
                (run_id, seq, name, json.dumps(data, ensure_ascii=False)),
            )
            if name in TERMINAL:
                self.db.execute(
                    "update runs set status = ?, finished_at = ? where id = ?",
                    (TERMINAL[name], stamp(), run_id),
                )
        return seq

    def events(self, run_id: str, after: int = 0) -> list[tuple[int, str, dict]]:
        return [
            (row["seq"], row["name"], json.loads(row["data"]))
            for row in self.db.execute(
                "select * from events where run_id = ? and seq > ? order by seq",
                (run_id, after),
            )
        ]

    def purge(self, days: float, now: datetime | None = None) -> int:
        """Delete the runs that finished more than `days` ago, with their events."""
        cutoff = stamp((now or datetime.now(UTC)) - timedelta(days=days))
        with self.db:
            return self.db.execute(
                "delete from runs where finished_at is not null and finished_at < ?",
                (cutoff,),
            ).rowcount
