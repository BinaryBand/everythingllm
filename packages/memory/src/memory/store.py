"""The long-term memory's store: facts the agent remembered and the chunks of files it
ingested, with their vectors, in one SQLite file, searched by cosine similarity.

A fact belongs to a scope: "global" (every workspace's) or a workspace's slug. A chunk
belongs to a workspace and the sandbox path it came from; ingesting a path again replaces
its chunks. Vectors are float32, normalised, so a dot product is the cosine; recall loads
the scope's vectors alone, ranks them with numpy and reads the text of the best, which for
the tens of thousands a home server holds takes tens of milliseconds and needs no vector
database.

The model each vector came from is kept (`meta.model`); `reembed` redoes them all when the
runner starts with another one, so every score is on one scale.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

GLOBAL = "global"
SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL,
  text TEXT NOT NULL,
  created TEXT NOT NULL,
  vector BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS facts_scope ON facts (scope);
CREATE TABLE IF NOT EXISTS chunks (
  workspace TEXT NOT NULL,
  path TEXT NOT NULL,
  seq INTEGER NOT NULL,
  text TEXT NOT NULL,
  ingested TEXT NOT NULL,
  vector BLOB NOT NULL,
  PRIMARY KEY (workspace, path, seq)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
FACT = "id, scope, text, created"  # a Fact's columns

# texts -> one normalised float32 row each
Embed = Callable[[Sequence[str]], np.ndarray]


@dataclass(frozen=True)
class Fact:
    id: int
    scope: str
    text: str
    created: str


@dataclass(frozen=True)
class Hit:
    """A recalled fact (`id`, its `scope`) or file chunk (`path`, its `seq`), and how close
    it is to the query (cosine, -1 to 1)."""

    score: float
    text: str
    id: int | None = None
    scope: str = ""
    path: str = ""
    seq: int = 0
    when: str = ""


class Full(Exception):
    """A scope holds as many facts as it may."""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def blob(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def marks(values: Sequence[object]) -> str:
    """The placeholders for `values` in an IN (...)."""
    return ",".join("?" * len(values))


class Store:
    """The memory in `path` (made on first use). Each call opens its own connection, so
    the runner's threads can share one Store; writes hold a lock, so a check and the write
    it decides on can't interleave with another's."""

    def __init__(self, path: Path):
        self.path = path
        self._write = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            yield db

    # --- the model the vectors came from ---

    def model(self) -> str | None:
        with self.db() as db:
            row = db.execute("SELECT value FROM meta WHERE key = 'model'").fetchone()
        return row[0] if row else None

    def reembed(self, model: str, embed: Embed) -> int:
        """Embed every fact and chunk again with `model` if they came from another; the
        number redone."""
        with self._write:
            if self.model() == model:
                return 0
            with self.db() as db:
                done = 0
                for table in ("facts", "chunks"):
                    rows = db.execute(f"SELECT rowid, text FROM {table}").fetchall()
                    vectors = embed([text for _, text in rows]) if rows else []
                    db.executemany(
                        f"UPDATE {table} SET vector = ? WHERE rowid = ?",
                        [(blob(v), k) for (k, _), v in zip(rows, vectors, strict=True)],
                    )
                    done += len(rows)
                db.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('model', ?)",
                    (model,),
                )
                return done

    # --- facts ---

    def same(self, scopes: Sequence[str], text: str) -> Fact | None:
        """A fact in one of `scopes` that already says `text` (ignoring case)."""
        with self.db() as db:
            return self._same(db, scopes, text)

    def _same(
        self, db: sqlite3.Connection, scopes: Sequence[str], text: str
    ) -> Fact | None:
        row = db.execute(
            f"SELECT {FACT} FROM facts WHERE scope IN ({marks(scopes)}) "
            "AND lower(text) = lower(?) LIMIT 1",
            (*scopes, text),
        ).fetchone()
        return Fact(*row) if row else None

    def add(
        self, scope: str, text: str, vector: np.ndarray, limit: int
    ) -> tuple[Fact, bool]:
        """Keep `text` in `scope`, and True; or the fact that already says it (in `scope`
        or, for a workspace's, global), and False. Full when `scope` holds `limit`."""
        with self._write, self.db() as db:
            found = self._same(db, [scope, GLOBAL], text)
            if found is not None:
                return found, False
            (count,) = db.execute(
                "SELECT count(*) FROM facts WHERE scope = ?", (scope,)
            ).fetchone()
            if count >= limit:
                raise Full(scope)
            created = now()
            cursor = db.execute(
                "INSERT INTO facts (scope, text, created, vector) VALUES (?, ?, ?, ?)",
                (scope, text, created, blob(vector)),
            )
            return Fact(int(cursor.lastrowid or 0), scope, text, created), True

    def delete(self, fact_id: int, scopes: Sequence[str]) -> Fact | None:
        """Delete the fact `fact_id` if it's in one of `scopes`; it, or None."""
        with self._write, self.db() as db:
            row = db.execute(
                f"DELETE FROM facts WHERE id = ? AND scope IN ({marks(scopes)}) "
                f"RETURNING {FACT}",
                (fact_id, *scopes),
            ).fetchone()
        return Fact(*row) if row else None

    def facts(self, scopes: Sequence[str], limit: int) -> tuple[list[Fact], int]:
        """The newest `limit` facts in `scopes`, newest first, and how many there are."""
        where = f"scope IN ({marks(scopes)})"
        with self.db() as db:
            rows = db.execute(
                f"SELECT {FACT} FROM facts WHERE {where} ORDER BY id DESC LIMIT ?",
                (*scopes, limit),
            ).fetchall()
            (count,) = db.execute(
                f"SELECT count(*) FROM facts WHERE {where}", tuple(scopes)
            ).fetchone()
        return [Fact(*row) for row in rows], count

    # --- file chunks ---

    def replace_file(
        self, workspace: str, path: str, texts: Sequence[str], vectors: np.ndarray
    ) -> None:
        """`path`'s chunks become `texts`."""
        ingested = now()
        with self._write, self.db() as db:
            db.execute(
                "DELETE FROM chunks WHERE workspace = ? AND path = ?", (workspace, path)
            )
            db.executemany(
                "INSERT INTO chunks (workspace, path, seq, text, ingested, vector) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (workspace, path, seq, text, ingested, blob(vector))
                    for seq, (text, vector) in enumerate(
                        zip(texts, vectors, strict=True)
                    )
                ],
            )

    def drop_file(self, workspace: str, path: str) -> int:
        """Drop the chunks of `path`, or of every file under it when it's a folder; how many
        files were dropped."""
        folder = path.rstrip("/") + "/"
        with self._write, self.db() as db:
            rows = db.execute(
                "DELETE FROM chunks WHERE workspace = ? "
                "AND (path = ? OR substr(path, 1, ?) = ?) RETURNING path",
                (workspace, path, len(folder), folder),
            ).fetchall()
        return len(set(rows))

    def files(self, workspace: str) -> list[tuple[str, int, str]]:
        """The workspace's ingested files: (path, chunks, when), by path."""
        with self.db() as db:
            return db.execute(
                "SELECT path, count(*), max(ingested) FROM chunks WHERE workspace = ? "
                "GROUP BY path ORDER BY path",
                (workspace,),
            ).fetchall()

    # --- recall ---

    def search(
        self, query: np.ndarray, scopes: Sequence[str], workspace: str | None, k: int
    ) -> list[Hit]:
        """The `k` facts in `scopes` and chunks of `workspace`'s files (none without one)
        closest to `query`, closest first."""
        hits: list[Hit] = []
        with self.db() as db:
            if scopes:
                rows = db.execute(
                    f"SELECT id, vector FROM facts WHERE scope IN ({marks(scopes)})",
                    tuple(scopes),
                ).fetchall()
                for fact_id, score in best(rows, query, k):
                    f = Fact(
                        *db.execute(
                            f"SELECT {FACT} FROM facts WHERE id = ?", (fact_id,)
                        ).fetchone()
                    )
                    hits.append(
                        Hit(score, f.text, id=f.id, scope=f.scope, when=f.created)
                    )
            if workspace:
                rows = db.execute(
                    "SELECT rowid, vector FROM chunks WHERE workspace = ?", (workspace,)
                ).fetchall()
                for rowid, score in best(rows, query, k):
                    path, seq, text, ingested = db.execute(
                        "SELECT path, seq, text, ingested FROM chunks WHERE rowid = ?",
                        (rowid,),
                    ).fetchone()
                    hits.append(Hit(score, text, path=path, seq=seq, when=ingested))
        return sorted(hits, key=lambda h: h.score, reverse=True)[:k]


def best(rows: list[tuple], query: np.ndarray, k: int) -> list[tuple[int, float]]:
    """The keys of the `k` (key, vector) rows closest to `query`, with their scores."""
    if not rows:
        return []
    matrix = np.frombuffer(b"".join(vector for _, vector in rows), dtype=np.float32)
    scores = matrix.reshape(len(rows), -1) @ np.asarray(query, dtype=np.float32)
    top = np.argsort(-scores)[:k]
    return [(rows[i][0], float(scores[i])) for i in top]
