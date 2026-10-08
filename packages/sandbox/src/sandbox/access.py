"""What a sandbox workspace's runs may reach beyond PyPI (Access): the public web, and a
model within a daily token budget (sandbox.models).

Off unless the user turned it on (the runner's op_access, the sandbox-access skill,
approved in AnythingLLM's own prompt). Each workspace's is kept in one JSON file, the
runner's access_file (SANDBOX_ACCESS, sandbox.workspace), as {workspace: Access}; a
workspace with none has no entry."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import hostrpc

from sandbox import models as model_access
from sandbox.errors import SandboxError

log = logging.getLogger("sandbox-runner")


@dataclass(frozen=True)
class Access:
    """What a workspace's runs may reach beyond PyPI: the public web (`web`), and a model
    (`models`, up to `daily_tokens` a day; sandbox.models). Off unless the user turned it
    on (op_access, the sandbox-access skill)."""

    web: bool = False
    models: bool = False
    daily_tokens: int = model_access.DAILY_TOKENS


NO_ACCESS = Access()
MAX_DAILY_TOKENS = 10_000_000


def valid_budget(daily_tokens: Any) -> bool:
    return (
        isinstance(daily_tokens, int)
        and not isinstance(daily_tokens, bool)
        and 0 < daily_tokens <= MAX_DAILY_TOKENS
    )


def read_access(file: Path, workspace: str) -> Access:
    """The workspace's access, from `file`; none when the file is missing or can't be read."""
    try:
        entry = json.loads(file.read_text()).get(workspace)
    except FileNotFoundError:
        return NO_ACCESS
    except (OSError, ValueError, AttributeError) as e:
        log.warning("couldn't read %s: %s", file, e)
        return NO_ACCESS
    if not isinstance(entry, dict):
        return NO_ACCESS
    budget = entry.get("daily_tokens")
    return Access(
        web=entry.get("web") is True,
        models=entry.get("models") is True,
        daily_tokens=budget if valid_budget(budget) else NO_ACCESS.daily_tokens,
    )


def write_access(file: Path, workspace: str, access: Access) -> None:
    """Keep the workspace's access in `file`, the others' as they are."""
    try:
        data = json.loads(file.read_text())
        if not isinstance(data, dict):
            data = {}
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as e:
        raise SandboxError(f"couldn't read the access settings: {e}") from None
    if access == NO_ACCESS:
        data.pop(workspace, None)
    else:
        data[workspace] = asdict(access)
    file.parent.mkdir(parents=True, exist_ok=True)
    hostrpc.atomic_write(file, json.dumps(data, indent=1, sort_keys=True) + "\n", 0o600)
