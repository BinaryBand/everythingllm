"""browser-runner's settings (Config) and the scope check every op starts with: where its
folders are, the egress profile its containers take addresses from, its ports and the
vault's key.

Config (environment):
  ANYTHINGLLM_STORAGE, PUBLIC_HOST  from host.env (the egress profile needs PUBLIC_HOST)
  BROWSER_ROOT           the sandbox's workspace folders, where downloads go (default
                         ~/.local/share/everythingllm/sandbox/workspaces, the sandbox's SANDBOX_ROOT)
  BROWSER_DATA           the runner's own folder (default ~/.local/share/everythingllm/browser):
                         profiles/<workspace>/, sockets/<slot>/, downloads/<workspace>/ and novnc/
                         (copied from the image by hostctl browser-images)
  BROWSER_LIVE_PORT      the live cards' port (default 8453), on LIVE_HOST (default 127.0.0.1)
  BROWSER_TAKEOVER_PORT  the take-over view's port (default 8454), on 127.0.0.1
  BROWSER_VAULT_KEY      the saved logins' key (default ~/.config/everythingllm/browser-vault.key,
                         made on first use); the vaults are in <data>/vault/
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import hostenv
from egress import config as egress_config
from hostrpc import RunnerError

PROFILE = "browser"  # egress.toml's profile, whose addresses the containers take
REPO = Path(__file__).resolve().parents[4]  # <repo>/packages/browser/src/browser/
CLIENT_PREFIX = "client-"  # the MCP gateway's clients' sandboxes, which have no browser
# As the sandbox's: workspace slugs and thread ids.
KEY_RE = re.compile(r"[a-z0-9_][a-z0-9_-]{0,99}")
VAULT_KEY = Path("~/.config/everythingllm/browser-vault.key").expanduser()
LIVE_PORT = 8453
TAKEOVER_PORT = 8454


@dataclass
class Config:
    root: Path
    data: Path
    ips: dict[str, str]  # slot -> address on the network
    network: str
    proxy: str  # the egress proxy's public port, as Chromium's --proxy-server
    pages_url: str = ""  # where the live cards are (https :8445), "" for no cards
    takeover_url: str = f"http://127.0.0.1:{TAKEOVER_PORT}/"
    live_port: int = LIVE_PORT
    takeover_port: int = TAKEOVER_PORT
    repo: Path = REPO
    vault_key: Path = VAULT_KEY

    @classmethod
    def from_env(cls) -> Config:
        get = os.environ.get
        host = get("PUBLIC_HOST")
        egress = egress_config.load()
        return cls(
            root=Path(
                get("BROWSER_ROOT", hostenv.data_dir() / "sandbox" / "workspaces")
            ),
            data=Path(get("BROWSER_DATA", hostenv.data_dir() / "browser")),
            ips=dict(egress.profiles[PROFILE].ips),
            network=egress.network,
            proxy=egress.public_url,
            pages_url=hostenv.pages_url(),
            takeover_url=f"https://{host}:{TAKEOVER_PORT}/"
            if host
            else f"http://127.0.0.1:{TAKEOVER_PORT}/",
            live_port=int(get("BROWSER_LIVE_PORT", LIVE_PORT)),
            takeover_port=int(get("BROWSER_TAKEOVER_PORT", TAKEOVER_PORT)),
            vault_key=Path(get("BROWSER_VAULT_KEY", VAULT_KEY)),
        )

    def sockets(self, slot: str) -> Path:
        return self.data / "sockets" / slot

    def downloads(self, workspace: str) -> Path:
        return self.data / "downloads" / workspace

    def profile(self, workspace: str) -> Path:
        return self.data / "profiles" / workspace


def check_scope(scope: Any) -> tuple[str, str]:
    """(workspace, thread) from a skill's scope, as the sandbox checks it."""
    if not isinstance(scope, dict):
        raise RunnerError("scope must be {workspace, thread}")
    workspace, thread = (
        str(scope.get("workspace") or ""),
        str(scope.get("thread") or ""),
    )
    for what, key in (("workspace", workspace), ("thread", thread)):
        if not KEY_RE.fullmatch(key):
            raise RunnerError(f"bad {what} '{key}'")
    if workspace.startswith(CLIENT_PREFIX) or scope.get("gateway"):
        raise RunnerError("the MCP gateway's clients have no browser")
    return workspace, thread
