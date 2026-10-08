"""This host and the AnythingLLM on it, as the services and hostctl see them: where things
are kept (`storage`, AnythingLLM's; `data_dir`, ours, laid out by kind; `site_dir`), a
service's socket (`socket_path`), the pages site's address (`pages_url`), the user's time
zone (`user_zone`), settings read from an env file (`env_values`), and the login for
AnythingLLM's internal API (`anythingllm_headers`). The EverythingLLM block of the system
prompt is hostenv.prompt.

Standard library only, so hostctl, which runs with any python3, can import it as the
services do. Talking to a service is hostrpc's.

Config (environment):
  ANYTHINGLLM_STORAGE  AnythingLLM's storage on the host (default /srv/anythingllm/storage)
  PUBLIC_HOST          the machine's HTTPS name, for pages_url ("" without it)
  USER_TIMEZONE        the user's time zone (default Europe/Stockholm)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# The folder in storage that holds our services' sockets, apart from AnythingLLM's own:
# a service's socket is <storage>/everythingllm/<folder>/runner.sock.
SOCKETS = "everythingllm"


class LoginFailed(Exception):
    """AnythingLLM's internal API couldn't be logged in to: refused, or not reached."""


def env_values(
    file: str | Path, names: Iterable[str], *, environ: bool = True
) -> dict[str, str]:
    """Just these KEY=value settings from an env file (AnythingLLM's .env, host.env), quotes
    dropped, so a service doesn't hold the others. With `environ` a non-empty value in the
    environment wins. An unreadable file reads as empty; names found nowhere are left out."""
    names = set(names)
    try:
        lines = Path(file).read_text().splitlines()
    except OSError:
        lines = []
    found = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip() in names:
            found[key.strip()] = value.strip().strip("'\"")
    if environ:
        found.update({n: os.environ[n] for n in names if os.environ.get(n)})
    return found


_tokens: dict[str, str] = {}  # AnythingLLM's API -> this process's login token


def anythingllm_headers(
    api: str, env_file: str | Path, *, fresh: bool = False
) -> dict[str, str]:
    """The headers for AnythingLLM's internal API (`<api>/...`, not the developer API's
    /v1): none while it has no password, else a Bearer token from logging in with the
    password in its .env (AUTH_TOKEN, set in the UI's Security settings). One login per
    process, since each one is logged; `fresh` logs in again, after a 401. Raises
    LoginFailed when it can't log in."""
    env = env_values(env_file, ("AUTH_TOKEN", "JWT_SECRET"), environ=False)
    if not (env.get("AUTH_TOKEN") and env.get("JWT_SECRET")):
        return {}
    if fresh or api not in _tokens:
        req = urllib.request.Request(
            f"{api.rstrip('/')}/request-token",
            json.dumps({"password": env["AUTH_TOKEN"]}).encode(),
            {"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                token = json.load(res).get("token")
        except urllib.error.HTTPError as e:
            raise LoginFailed(
                f"AnythingLLM refused the password in {env_file} ({e.code})"
            ) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LoginFailed(f"couldn't log in to AnythingLLM at {api}: {e}") from None
        if not token:
            raise LoginFailed(f"AnythingLLM refused the password in {env_file}")
        _tokens[api] = token
    return {"Authorization": f"Bearer {_tokens[api]}"}


PAGES_PORT = 8445  # the pages site's HTTPS port, where the live cards are routed too


def pages_url() -> str:
    """The pages site's public URL, from PUBLIC_HOST; "" without it (and no live cards)."""
    host = os.environ.get("PUBLIC_HOST", "").strip()
    return f"https://{host}:{PAGES_PORT}/" if host else ""


def user_zone() -> ZoneInfo:
    """The user's time zone (USER_TIMEZONE), or Europe/Stockholm when it's unset or not one."""
    try:
        return ZoneInfo(os.environ.get("USER_TIMEZONE") or "Europe/Stockholm")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Europe/Stockholm")


def storage() -> Path:
    """AnythingLLM's storage directory as the host sees it (from host.env)."""
    return Path(os.environ.get("ANYTHINGLLM_STORAGE", "/srv/anythingllm/storage"))


def data_dir() -> Path:
    """EverythingLLM's own data on the host: what only host services read or write, kept
    out of AnythingLLM's storage, which the container mounts. By kind:

      venvs/<name>/        the host services' venvs
      venvs/<x>-ctr/       a service container's venv and uv cache (venv/, uv-cache/)
      pages/public/        the pages site Caddy serves; pages/entries/, the Zola entries
      sandbox/workspaces/  the sandbox's folders, per workspace: threads/, project/, shared/
      sandbox/public/      each sandbox workspace's /public, served as it is on :8447
      browser/             browser-runner's: profiles/<workspace>/ (each workspace's
                           browser profile), sockets/<slot>/ (each browser's), downloads/,
                           novnc/ and vault/ (the saved logins, sealed)
      research/runs/       the deep-research run log and live runs' markers
      agents/runs/         the delegations' run log and live runs' markers; agents/ also
                           keeps the one-offs made (once.json) and the research runs
                           followed for their chats (followed.json)
      relay/               the Nilson relay's database
      hostctl/skills/      what the UI set in each skill deploy took out (hostctl.sync)"""
    return Path("~/.local/share/everythingllm").expanduser()


def site_dir() -> Path:
    """The pages site's folder on the host, which Caddy serves on :8445."""
    return data_dir() / "pages" / "public"


def socket_path(folder: str, env: str) -> Path:
    """A service's socket on the host: $<env>, else <storage>/everythingllm/<folder>/runner.sock."""
    return Path(os.environ.get(env) or storage() / SOCKETS / folder / "runner.sock")
