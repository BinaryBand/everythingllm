"""`uv run hostctl relay-setup`'s first step: the Nilson relay's settings file.

Makes ~/.config/everythingllm/relay.env (mode 600) when it's missing, for the relay's
optional ntfy settings, which are secrets: the relay needs no key of its own, since it takes
the client's AnythingLLM key. The file stays outside the repo, which the AnythingLLM
container mounts. A file from before that still holds ANYTHINGLLM_API_KEY or RELAY_TOKEN is
left as it is, with a note that they're no longer read. Standard library only, like the rest
of hostctl.

    python3 -m hostctl.relay_env [path]   # with hostctl and apps on PYTHONPATH, as appctl does
"""

import os
import sys
from pathlib import Path

from hostctl.units import env_file

DEFAULT = Path("~/.config/everythingllm/relay.env").expanduser()
UNUSED = ("ANYTHINGLLM_API_KEY", "RELAY_TOKEN")

TEMPLATE = """\
# The Nilson relay's settings (packages/relay; see the README's "Nilson relay"). Mode 600.
# The relay takes each client's own AnythingLLM API key, so it needs none here.
# Optional: the ntfy topic told about finished runs (e.g. https://ntfy.sh/nilson-<random>),
# and its token.
NTFY_URL=
NTFY_TOKEN=
"""


def main() -> None:
    path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else DEFAULT
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(TEMPLATE)
        print(f"made {path}")
    path.chmod(0o600)
    if unused := [k for k in UNUSED if k in env_file(path)]:
        print(
            f"relay: {' and '.join(unused)} in {path} are no longer read (the relay takes"
            " the client's own AnythingLLM key); you can delete them"
        )
    print(f"{path} has what the relay needs")


if __name__ == "__main__":
    main()
