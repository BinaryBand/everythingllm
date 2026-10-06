"""`make relay-setup`'s first step: the Nilson relay's secrets file.

Makes ~/.config/anything/relay.env (mode 600) when it's missing, with a fresh RELAY_TOKEN and
an empty ANYTHINGLLM_API_KEY, and exits 1 until that key is filled in, so the unit isn't
started into a crash loop. The file stays outside the repo, which the AnythingLLM container
mounts. Standard library only, run with the system python3.

    python3 scripts/relay_env.py [path]
"""

import os
import secrets
import sys
from pathlib import Path

DEFAULT = Path("~/.config/anything/relay.env").expanduser()

TEMPLATE = """\
# The Nilson relay's secrets (src/relay; see the README's "Nilson relay"). Mode 600.
# A developer API key from AnythingLLM's Settings > Developer API.
ANYTHINGLLM_API_KEY=
# The token Nilson sends the relay; made by `make relay-setup`.
RELAY_TOKEN={token}
# Optional: the ntfy topic told about finished runs (e.g. https://ntfy.sh/nilson-<random>),
# and its token.
NTFY_URL=
NTFY_TOKEN=
"""


def values(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            out[key.strip()] = value.strip()
    return out


def main() -> None:
    path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else DEFAULT
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(TEMPLATE.format(token=secrets.token_urlsafe(32)))
        print(f"made {path} with a new RELAY_TOKEN")
    path.chmod(0o600)
    found = values(path)
    missing = [k for k in ("ANYTHINGLLM_API_KEY", "RELAY_TOKEN") if not found.get(k)]
    if missing:
        sys.exit(
            f"relay: fill in {' and '.join(missing)} in {path}, then run make relay-setup again"
        )
    print(f"{path} has what the relay needs")


if __name__ == "__main__":
    main()
