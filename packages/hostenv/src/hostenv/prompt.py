"""The system prompt's EverythingLLM block: what `update-prompt` writes into a workspace's
prompt (agents-runner's update_prompt), what deploy sets as the default for new workspaces
(hostctl.sync), and the version AnythingLLM compares it with.

A workspace's prompt is AnythingLLM's, edited in its UI. Ours goes in as one block, marked
with the version of anythingllm/system-prompt.md it was written from:

  <everythingllm version="a3f9c2e1">
  …system-prompt.md…
  (a line asking the model to say so once when the version is behind {everythingllm_version})
  </everythingllm>

Deploy keeps the static System Prompt Variable everythingllm_version at the repo's version,
which AnythingLLM expands at chat time, so a workspace whose block is behind sees it. The
skill replaces only the block; text around it stays. Which workspaces are behind is
hostctl.prompt's check. Importing it reads nothing.
"""

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
REPO_PROMPT = ROOT / "anythingllm" / "system-prompt.md"
VARIABLE = "everythingllm_version"
# Delegation's role workspaces: agents-runner sets their prompts (agents.profiles).
DELEGATED = "agents-"
BLOCK = re.compile(
    r'<everythingllm version="([0-9a-f]+)">\n.*?\n</everythingllm>', re.DOTALL
)
NOTICE = (
    "This block was written for EverythingLLM {version}; the current version is "
    "{{" + VARIABLE + "}}. If they differ, tell the user once in this chat that this "
    "workspace's prompt is out of date and that the update-prompt skill refreshes it."
)
OWN = "Your own instructions:"


def version(text: str) -> str:
    """The prompt's version: a short hash of its text."""
    return hashlib.sha256(text.strip().encode()).hexdigest()[:8]


def block(text: str) -> str:
    """The repo prompt as the block a workspace's prompt carries."""
    v = version(text)
    notice = NOTICE.format(version=v)
    return (
        f'<everythingllm version="{v}">\n{text.strip()}\n\n{notice}\n</everythingllm>'
    )


def written_version(prompt: str | None) -> str | None:
    """The version a prompt's block was written from, or None if it has no block."""
    m = BLOCK.search(prompt or "")
    return m.group(1) if m else None


def splice(existing: str | None, text: str) -> str:
    """`existing` with its block replaced by the one for `text`. A prompt that's empty, or
    the bare repo prompt deploys used to set on every workspace, becomes the block; one
    with no block keeps its text after it, under OWN."""
    new = block(text)
    existing = (existing or "").strip()
    if not existing or existing == text.strip():
        return new
    m = BLOCK.search(existing)
    if m:
        return existing[: m.start()] + new + existing[m.end() :]
    return f"{new}\n\n{OWN}\n{existing}"
