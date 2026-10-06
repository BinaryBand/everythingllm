"""One JSON line per research run in <dir>/YYYY-MM.jsonl: what was asked, how it ended, its
stats and every progress line. AnythingLLM keeps only a chat's final reply, so this is the
record the audit MCP server reads.

The line is written when the run ends, so while it runs it has a marker in
<dir>/running/<id>.json instead: its question, when it started and `stale_ms`. A thread
touches the marker every minute while the run is alive. A restart of research-runner kills
its runs without letting them write their lines, so the runner, when it starts, moves every
marker into the log as an "interrupted" line (none of them can be its own). A marker quiet
for stale_ms belongs to a run that's gone too; the audit (checks.py) and
tools/research_guard.py read it so, taking stale_ms from the marker.
"""

import json
import os
import secrets
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

MAX_EVENTS = 2000
STALE_MS = 3 * 60_000  # a live run's marker is touched every TOUCH seconds
TOUCH = 60


def iso(t: float) -> str:
    """As JavaScript's toISOString, which the older lines use: 2026-10-03T12:00:00.000Z."""
    return (
        datetime.fromtimestamp(t, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def month_file(dir: Path, started: str) -> Path:
    return dir / f"{started[:7]}.jsonl"


def append_line(dir: Path, started: str, record: dict) -> Path:
    file = month_file(dir, started)
    dir.mkdir(parents=True, exist_ok=True)
    with file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return file


def sweep_interrupted(
    dir: Path, now: float | None = None, everything: bool = False
) -> list[str]:
    """Move the markers of runs that were killed into the log as interrupted runs: every
    marker when `everything` (the runner starting), else those quiet for their stale_ms.
    Returns the questions of the runs swept."""
    now = time.time() if now is None else now
    swept = []
    for file in sorted((dir / "running").glob("*.json")):
        try:
            mtime = file.stat().st_mtime
            marker = json.loads(file.read_text(encoding="utf-8"))
            if not everything and (now - mtime) * 1000 < marker.get(
                "stale_ms", STALE_MS
            ):
                continue
            run = {k: v for k, v in marker.items() if k != "stale_ms"}
            started = run.get("started") or iso(mtime)
            seconds = max(
                0,
                round(mtime - datetime.fromisoformat(started).timestamp()),
            )
            append_line(
                dir,
                started,
                {
                    **run,
                    "started": started,
                    "status": "interrupted",
                    "seconds": seconds,
                    "events": [],
                },
            )
            file.unlink()
            swept.append(str(run.get("question", "")))
        except (OSError, ValueError):
            continue  # unreadable, or swept meanwhile; leave it
    return swept


class RunLog:
    def __init__(self, dir: Path, now=time.time):
        self.dir = Path(dir)
        self.now = now
        self.started = now()
        self.started_iso = iso(self.started)
        stamp = self.started_iso.replace(":", "-").replace(".", "-")
        self.marker = self.dir / "running" / f"{stamp}-{secrets.token_hex(3)}.json"
        self.events: list[list] = []
        self._stop = threading.Event()

    def elapsed(self) -> int:
        return round(self.now() - self.started)

    def start(self, record: dict) -> None:
        """Note that the run has begun: its marker, kept fresh until write()."""
        try:
            sweep_interrupted(self.dir, self.now())
            self.marker.parent.mkdir(parents=True, exist_ok=True)
            note = {"started": self.started_iso, **record, "stale_ms": STALE_MS}
            self.marker.write_text(
                json.dumps(note, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except OSError:
            return  # the run matters more than its marker
        threading.Thread(target=self._touch, name="runlog-touch", daemon=True).start()

    def _touch(self) -> None:
        while not self._stop.wait(TOUCH):
            try:
                os.utime(self.marker)
            except OSError:
                pass  # deleted meanwhile; the final line still gets written

    def event(self, message: str) -> None:
        """Record a progress line."""
        if len(self.events) < MAX_EVENTS:
            self.events.append([self.elapsed(), str(message)])

    def write(self, record: dict) -> Path:
        """Append the run's line and drop its marker; returns the file written."""
        self._stop.set()
        file = append_line(
            self.dir,
            self.started_iso,
            {
                "started": self.started_iso,
                "seconds": self.elapsed(),
                **record,
                "events": self.events,
            },
        )
        self.marker.unlink(missing_ok=True)
        return file
