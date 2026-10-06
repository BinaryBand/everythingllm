"""`uv run hostctl gateway-setup`'s first step: the MCP gateway's tokens file.

Makes ~/.config/everythingllm/gateway.env (mode 600) when it's missing, with a fresh token
for the first client, Claude Code, and exits 1 if the file has no token, so the unit isn't
started into a crash loop. A client is a GATEWAY_TOKEN_<NAME> line: add one per client, and
delete a client's line to revoke it (then restart the gateway). Tokens are never printed.
The file stays outside the repo, which the AnythingLLM container mounts. Standard library
only, like the rest of hostctl.

    python3 -m hostctl.gateway_env [path]   # with hostctl and apps on PYTHONPATH, as appctl does
"""

import os
import secrets
import sys
from pathlib import Path

from hostctl.units import env_file

DEFAULT = Path("~/.config/everythingllm/gateway.env").expanduser()

PREFIX = "GATEWAY_TOKEN_"

TEMPLATE = """\
# The MCP gateway's client tokens (packages/gateway; see the README's "MCP gateway"). Mode 600.
# One GATEWAY_TOKEN_<NAME> per client; delete a line to revoke that client, then restart
# the gateway (systemctl --user restart gateway). A client's tools are its grant in
# packages/gateway/src/gateway/grants.toml; one with no grant gets none. Make a new token with
# python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
GATEWAY_TOKEN_CLAUDE_CODE={token}
"""


def main() -> None:
    path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else DEFAULT
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(TEMPLATE.format(token=secrets.token_urlsafe(32)))
        print(f"made {path} with a token for claude-code")
    path.chmod(0o600)
    clients = [k for k, v in env_file(path).items() if k.startswith(PREFIX) and v]
    if not clients:
        sys.exit(
            f"gateway: add a {PREFIX}<NAME>=<token> line to {path}, then run uv run hostctl gateway-setup again"
        )
    names = ", ".join(
        sorted(k.removeprefix(PREFIX).lower().replace("_", "-") for k in clients)
    )
    print(f"{path} has tokens for {names}")


if __name__ == "__main__":
    main()
