"""This host and the AnythingLLM on it, as the services and hostctl see them: where things
are kept (`storage`, AnythingLLM's, and its .env, `anythingllm_env`; `data_dir`, ours, laid
out by kind; `site_dir`), a service's socket (`socket_path`), the pattern of a workspace's
slug (`WORKSPACE`) and a scheduled job's (`JOBS`), a remembered fact's text (`fact_text`)
and an id the model gave (`whole_number`), the sandbox's folders (`sandbox_root`,
`sandbox_mounts`), the pages site's address (`pages_url`), the user's time zone
(`user_zone`), settings read from an env file (`env_values`), and the login for
AnythingLLM's internal API (`anythingllm_headers`). The EverythingLLM block of the system
prompt is hostenv.prompt.

Standard library only, so hostctl, which runs with any python3, can import it as the
services do. Talking to a service is hostrpc's.

Config (environment):
  ANYTHINGLLM_STORAGE  AnythingLLM's storage on the host (default /srv/anythingllm/storage)
  ANYTHINGLLM_ENV      AnythingLLM's .env (default <storage>/.env)
  PUBLIC_HOST          the machine's HTTPS name, for pages_url ("" without it)
  USER_TIMEZONE        the user's time zone (default Europe/Stockholm)
  SANDBOX_ROOT         the sandbox's workspace folders (default
                       ~/.local/share/everythingllm/sandbox/workspaces)
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# The folder in storage that holds our services' sockets, apart from AnythingLLM's own:
# a service's socket is <storage>/everythingllm/<folder>/runner.sock.
SOCKETS = "everythingllm"
SOCKET = "runner.sock"  # a service's socket file in its folder there

# A workspace's slug in AnythingLLM, as the sandbox and the runners take it (thread ids too).
WORKSPACE = r"[a-z0-9_][a-z0-9_-]{0,99}"
WORKSPACE_RE = re.compile(WORKSPACE)  # use with fullmatch

# A scheduled job's workspace in a skill's scope (anythingllm/agent-skills/_lib/scope.js):
# it has none, and no one asked for what it does.
JOBS = "_jobs"

MAX_FACT = 500  # characters in a remembered fact
# What a remembered fact (AnythingLLM's saved memories, the long-term memory's facts) can't
# hold: control characters, and the invisible ones that would make it read differently where
# it's shown than in the prompt: C1, bidi marks, overrides and isolates, zero-width spaces
# and the BOM (not ZWJ/ZWNJ, which emoji and scripts need).
HIDDEN = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200b\u200e\u200f\u202a-\u202e\u2060-\u2069\ufeff]"
)


def one_line(text: object) -> str:
    """`text` on one line, its runs of white space one space each."""
    return " ".join(str(text or "").split())


def fact_text(value: object) -> str:
    """A fact to remember as the model gave it, on one line; ValueError, saying what's
    wrong, unless it's short plain text."""
    if value is not None and not isinstance(value, str):
        raise ValueError("give the text to remember as text: one short fact")
    text = one_line(value)
    if not text:
        raise ValueError("give the text to remember: one short fact about the user")
    if HIDDEN.search(text):
        raise ValueError(
            "the text can't have control characters or invisible ones (bidi marks, "
            "zero-width spaces)"
        )
    if len(text) > MAX_FACT:
        raise ValueError(
            f"a fact is one short sentence, at most {MAX_FACT} characters; this is "
            f"{len(text)}. Shorten it, or save two."
        )
    return text


def whole_number(value: object) -> int | None:
    """An id or count the model gave, as an int: a whole number, or its digits as text;
    None for anything else, since int() would make true 1 and 12.7 12, and act on that one."""
    if isinstance(value, str):
        value = value.strip()
        return int(value) if re.fullmatch(r"[0-9]{1,18}", value) else None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None  # also inf and nan
    return int(value)


class LoginFailed(Exception):
    """AnythingLLM's internal API couldn't be logged in to: refused, or not reached."""


def env_values(
    file: str | Path, names: Iterable[str] | None = None, *, environ: bool = True
) -> dict[str, str]:
    """Just these KEY=value settings from an env file (AnythingLLM's .env, host.env), quotes
    dropped, so a service doesn't hold the others; every setting when `names` is None. With
    `environ` a non-empty value in the environment wins. An unreadable file reads as empty;
    names found nowhere are left out."""
    wanted = None if names is None else set(names)
    try:
        lines = Path(file).read_text().splitlines()
    except OSError:
        lines = []
    found = {}
    for line in lines:
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and not key.startswith("#") and (wanted is None or key in wanted):
            found[key] = value.strip().strip("'\"")
    if environ and wanted:
        found.update({n: os.environ[n] for n in wanted if os.environ.get(n)})
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
    if not fresh and api in _tokens:
        return {"Authorization": f"Bearer {_tokens[api]}"}
    env = env_values(env_file, ("AUTH_TOKEN", "JWT_SECRET"), environ=False)
    if not (env.get("AUTH_TOKEN") and env.get("JWT_SECRET")):
        return {}
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


def anythingllm_env() -> Path:
    """AnythingLLM's .env (ANYTHINGLLM_ENV, else the one in its storage): its model keys and
    the password its internal API logs in with."""
    return Path(os.environ.get("ANYTHINGLLM_ENV") or storage() / ".env")


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
      memory/              the long-term memory's store (memory.db) and model (models/)
      hostctl/skills/      what the UI set in each skill deploy took out (hostctl.sync)"""
    return Path("~/.local/share/everythingllm").expanduser()


def site_dir() -> Path:
    """The pages site's folder on the host, which Caddy serves on :8445."""
    return data_dir() / "pages" / "public"


def sandbox_root() -> Path:
    """The sandbox's workspace folders on the host (SANDBOX_ROOT), one per workspace."""
    return Path(os.environ.get("SANDBOX_ROOT") or data_dir() / "sandbox" / "workspaces")


def sandbox_mounts(home: Path, workspace: str, thread: str) -> dict[str, Path]:
    """The folders a sandbox run of `workspace`'s chat `thread` writes, by where the run
    sees them, as they are on the host under the workspace's folder `home`."""
    return {
        "/work": home / "threads" / thread,
        "/project": home / "project",
        f"/shared/{workspace}": home / "shared",
    }


def socket_path(folder: str, env: str) -> Path:
    """A service's socket on the host: $<env>, else <storage>/everythingllm/<folder>/runner.sock."""
    return Path(os.environ.get(env) or storage() / SOCKETS / folder / SOCKET)
