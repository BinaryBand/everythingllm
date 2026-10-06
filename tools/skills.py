"""Write the agent skills that forward one op to a host service, from the fronts' `skills`.

    uv run --all-packages python tools/skills.py           # write them (make skills)
    uv run --all-packages python tools/skills.py --check   # exit 1 if they're stale

See hostrpc.skillgen. Not standard-library only (it imports the fronts), so it runs in the
dev venv rather than with the system python3, unlike tools/sync.py.
"""

import argparse
import sys
from pathlib import Path

from hostrpc import skillgen

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only say what's stale")
    args = parser.parse_args()
    if args.check:
        if stale := skillgen.stale(ROOT):
            sys.exit(
                "generated skills are stale; run `make skills`:\n  "
                + "\n  ".join(stale)
            )
        return
    for path in skillgen.write(ROOT):
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
