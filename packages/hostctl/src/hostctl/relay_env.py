"""`uv run hostctl relay-setup`'s and `research-setup`'s first step: the Nilson relay's
settings file.

Makes ~/.config/everythingllm/relay.env (mode 600) when it's missing, for the relay's
optional ntfy settings, which are secrets: the relay needs no key of its own, since it takes
the client's AnythingLLM key. research-runner reads the same ntfy settings, to tell the app
when a research run ends, so its container needs the file too. The file stays outside the repo, which the AnythingLLM
container mounts. Standard library only, like the rest of hostctl.

    python3 -m hostctl.relay_env [path]   # with hostctl on PYTHONPATH, as appctl does
"""

import sys
from pathlib import Path

from hostctl import units

DEFAULT = Path("~/.config/everythingllm/relay.env").expanduser()

TEMPLATE = """\
# The Nilson relay's settings (packages/relay; see the README's "Nilson relay"). Mode 600.
# The relay takes each client's own AnythingLLM API key, so it needs none here.
# KEY=value lines without quotes: podman passes each value to the relay as it is.
# Optional: the ntfy topic told about finished runs (e.g. https://ntfy.sh/nilson-<random>),
# and its token. research-runner tells the same topic about ended research runs.
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
