"""The MCP gateway's tokens file: `uv run hostctl gateway-setup`'s first step, and
`uv run hostctl gateway-client <name>`, which adds a client.

gateway-setup makes ~/.config/everythingllm/gateway.env (mode 600) when it's missing, with a
fresh token for the first client, Claude Code, and exits 1 if the file has no token, so the
unit isn't started into a crash loop. A client is a GATEWAY_TOKEN_<NAME> line: delete a
client's line to revoke it (then restart the gateway). The file stays outside the repo,
which the AnythingLLM container mounts. Standard library only, like the rest of hostctl.

gateway-client adds a client's line with a fresh token when the file has none for it (it
never replaces one), then prints the `claude mcp add` command for that client, token and
all: the user runs it on purpose, and it's the one place a token is printed. The client gets
no tools until grants.toml grants it some and the gateway restarts, which it says.

    python3 -m hostctl.gateway_env [path]   # with hostctl and apps on PYTHONPATH, as appctl does

Config (environment):
  PUBLIC_HOST  the machine's HTTPS name in the printed command (from host.env, which hostctl reads)
"""

import os
import re
import secrets
import shlex
import sys
from pathlib import Path

import apps
import tomllib

from hostctl.units import ROOT, env_file

DEFAULT = Path("~/.config/everythingllm/gateway.env").expanduser()
GRANTS = ROOT / "packages" / "gateway" / "src" / "gateway" / "grants.toml"

PREFIX = "GATEWAY_TOKEN_"
# A client's name, as gateway.app.CLIENT_RE has it (a test holds them equal): it names the
# client's sandbox workspace too, client-<name>.
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

HEADER = """\
# The MCP gateway's client tokens (packages/gateway; see the README's "MCP gateway"). Mode 600.
# One GATEWAY_TOKEN_<NAME> per client (uv run hostctl gateway-client <name> adds one); delete
# a line to revoke that client, then restart the gateway (systemctl --user restart gateway).
# A client's tools are its grant in packages/gateway/src/gateway/grants.toml; one with no
# grant gets none.
"""
TEMPLATE = HEADER + "GATEWAY_TOKEN_CLAUDE_CODE={token}\n"


def key(name: str) -> str:
    """The gateway.env key of a client's token: claude-code's is GATEWAY_TOKEN_CLAUDE_CODE."""
    return PREFIX + name.upper().replace("-", "_")


def create(path: Path, text: str) -> None:
    """Write a new file only its owner can read, in a folder only its owner can enter."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def main() -> None:
    path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else DEFAULT
    if not path.exists():
        create(path, TEMPLATE.format(token=secrets.token_urlsafe(32)))
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


def granted(name: str, grants: Path = GRANTS) -> list[str] | None:
    """The groups grants.toml gives a client, or None when it has no entry."""
    try:
        with grants.open("rb") as f:
            clients = tomllib.load(f).get("clients", {})
    except (OSError, tomllib.TOMLDecodeError):
        return None
    grant = clients.get(name)
    return list(grant.get("tools", [])) if isinstance(grant, dict) else None


def add_client(name: str, path: Path = DEFAULT, grants: Path = GRANTS) -> None:
    """Give client `name` a token in gateway.env unless it has one, and print how to add
    the gateway to Claude Code as that client."""
    if not NAME_RE.fullmatch(name):
        sys.exit(
            f"gateway-client: '{name}' isn't a client name: use at most 63 lowercase "
            "letters, digits and hyphens, not starting or ending with a hyphen"
        )
    env = key(name)
    token = env_file(path).get(env)
    if token == "":
        sys.exit(f"gateway-client: {path} has an empty {env}= line; delete it first")
    if path.exists():
        path.chmod(0o600)  # before a new token goes in
    if token:
        print(f"{path} already has a token for {name}; it's kept")
    elif path.exists():
        token = secrets.token_urlsafe(32)
        text = path.read_text()
        with path.open("a") as f:
            f.write(
                ("" if not text or text.endswith("\n") else "\n") + f"{env}={token}\n"
            )
        print(f"added a token for {name} to {path}")
    else:
        token = secrets.token_urlsafe(32)
        create(path, f"{HEADER}{env}={token}\n")
        print(f"made {path} with a token for {name}")

    [mapping] = apps.load()["gateway"].serve
    host = os.environ.get("PUBLIC_HOST") or "<PUBLIC_HOST>"
    command = shlex.join(
        ["claude", "mcp", "add", "--transport", "http", "everythingllm",
         f"https://{host}:{mapping.https}/mcp",
         "--header", f"Authorization: Bearer {token}"]
    )  # fmt: skip
    print(
        "\nOn the client's machine, add the gateway to Claude Code with this command. It "
        "holds the token: keep it out of anything you share.\n"
    )
    print(f"    {command}\n")
    groups = granted(name, grants)
    if groups is None:
        print(
            f"{name} has no grant, so it gets no tools. Add one to {grants} (its comments "
            f'list the groups):\n\n    [clients.{name}]\n    tools = ["sites", ...]\n'
        )
    else:
        print(f"Its grant in grants.toml: {', '.join(groups) or 'no groups'}.")
    print(
        "The gateway reads the tokens and grants when it starts, so restart it: "
        "systemctl --user restart gateway"
    )


if __name__ == "__main__":
    main()
