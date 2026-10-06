"""Before research-runner restarts: is a deep-research run going? A restart kills it
without a report (a run writes its log line only at the end), so this lists live runs and asks.

  python3 src/tools/research_guard.py   exit 0 to go ahead, 1 to stop

A run is live while its marker in ~/.local/share/everythingllm/research/runs/running/
has been touched
within the marker's stale_ms (see src/mcps/research/src/research/runlog.py). With no terminal to
ask, it stops unless FORCE=1. Used by `make research-setup` and units.py. AnythingLLM's own
restarts don't need it: runs live in research-runner, not in AnythingLLM.

Standard library only, run with the system `python3`, like sync.py.
"""

import json
import os
import sys
import time
from pathlib import Path

SERVICE = "research-runner.service"


DATA = Path.home() / ".local" / "share" / "everythingllm"  # hostrpc.data_dir()


def live_runs(data: Path) -> list[dict]:
    runs = []
    for f in sorted((data / "research" / "runs" / "running").glob("*.json")):
        try:
            run, quiet = json.loads(f.read_text()), time.time() - f.stat().st_mtime
        except (OSError, ValueError):
            continue
        if quiet * 1000 < run.get(
            "stale_ms", 3 * 60_000
        ):  # research.runlog.STALE_MS (stdlib only here)
            runs.append(run)
    return runs


def ok_to_restart(data: Path = DATA) -> bool:
    runs = live_runs(data)
    if not runs:
        return True
    print(
        f"Deep-research runs going ({len(runs)}); restarting research-runner kills them without a report:",
        file=sys.stderr,
    )
    for r in runs:
        print(
            f"  - started {r.get('started', '?')[:16]} UTC: {str(r.get('question', ''))[:100]}",
            file=sys.stderr,
        )
    if os.environ.get("FORCE") == "1":
        print("FORCE=1: restarting anyway.", file=sys.stderr)
        return True
    if not sys.stdin.isatty():
        print(
            "Not restarting. Wait for them, or run again with FORCE=1.", file=sys.stderr
        )
        return False
    return input("Restart anyway? [y/N] ").strip().lower() in ("y", "yes")


def main() -> None:
    sys.exit(0 if ok_to_restart() else 1)


if __name__ == "__main__":
    main()
