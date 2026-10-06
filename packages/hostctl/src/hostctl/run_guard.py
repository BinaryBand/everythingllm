"""Before a service that holds long runs restarts: is one of its runs going? A restart kills
it without its result (a run writes its log line only at the end), so this lists live runs
and asks.

  python3 -m hostctl.run_guard <service>   exit 0 to go ahead, 1 to stop

The apps registry (packages/apps, `guard`) says which services hold runs and where their
run logs are; GUARDED is that, by unit. A run is live while
its marker in <run log>/running/ has been touched within the marker's stale_ms (see
packages/runs/src/runs/runlog.py). With no terminal to ask, it stops unless FORCE=1. Used
by appctl.py (`make <app>-setup`) and units.py. AnythingLLM's own restarts
don't need it: the runs live in the services, not in AnythingLLM.

Standard library only, run with the system `python3`, like sync.py; the registry's reader
is too.
"""

import json
import os
import sys
import time
from pathlib import Path

import apps  # the registry's reader, standard library only

DATA = Path.home() / ".local" / "share" / "everythingllm"  # hostrpc.data_dir()
# service: (its run log in DATA, what its runs are called)
GUARDED = {unit: (g.runs, g.noun) for unit, g in apps.guarded().items()}


def live_runs(runlog: Path) -> list[dict]:
    runs = []
    for f in sorted((runlog / "running").glob("*.json")):
        try:
            run, quiet = json.loads(f.read_text()), time.time() - f.stat().st_mtime
        except (OSError, ValueError):
            continue
        if quiet * 1000 < run.get(
            "stale_ms", 3 * 60_000
        ):  # runs.runlog.STALE_MS (stdlib only here)
            runs.append(run)
    return runs


def ok_to_restart(service: str, data: Path = DATA) -> bool:
    folder, noun = GUARDED[service]
    runs = live_runs(data / folder)
    if not runs:
        return True
    name = service.removesuffix(".service")
    print(
        f"{noun} going ({len(runs)}); restarting {name} kills them without a result:",
        file=sys.stderr,
    )
    for r in runs:
        what = r.get("subject") or r.get("question") or ""
        print(
            f"  - started {r.get('started', '?')[:16]} UTC: {str(what)[:100]}",
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
    if len(sys.argv) != 2 or sys.argv[1] not in GUARDED:
        sys.exit(f"usage: run_guard.py {{{','.join(GUARDED)}}}")
    sys.exit(0 if ok_to_restart(sys.argv[1]) else 1)


if __name__ == "__main__":
    main()
