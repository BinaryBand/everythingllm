"""`uv run hostctl research-setup`'s first step: research-runner's ntfy settings file.

Makes ~/.config/everythingllm/ntfy.env (mode 600) when it's missing, for research-runner's
optional ntfy topic and token (secrets), with which it tells the Nilson app a research run
ended. The file stays outside the repo, which the AnythingLLM container mounts. The Nilson
relay (its own repo) has the same two settings in its own file. Standard library only, like
the rest of hostctl.

    python3 -m hostctl.ntfy_env [path]   # with hostctl on PYTHONPATH, as appctl does
"""

import sys
from pathlib import Path

from hostctl import units

DEFAULT = Path("~/.config/everythingllm/ntfy.env").expanduser()

TEMPLATE = """\
# research-runner's ntfy settings (see the README's "Deep research"). Mode 600.
# KEY=value lines without quotes: podman passes each value to research-runner as it is.
# Optional: the ntfy topic told about ended research runs (e.g. https://ntfy.sh/nilson-<random>),
# and its token. Use the Nilson relay's topic, so the app follows one.
NTFY_URL=
NTFY_TOKEN=
"""


def main() -> None:
    path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else DEFAULT
    if not path.exists():
        units.create(path, TEMPLATE)
        print(f"made {path}")
    path.chmod(0o600)


if __name__ == "__main__":
    main()
