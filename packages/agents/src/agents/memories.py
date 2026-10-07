"""agents-runner's side of AnythingLLM's saved memories, for the memories skill: list a
chat's (global and its workspace's), save one, or forget one, each at once: forgetting gives
back the text, so a mistake is undone by saving it again. The README's "Saved memories" has
the rules.

Config (environment, from host.env through the unit):
  USER_TIMEZONE  the user's time zone, for the "last used" times `list` shows
"""

import re
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from hostrpc import RunnerError

from agents.anythingllm import InternalAPI, NotFound
from agents.jobs import DEFAULT_TIMEZONE, local, when, whole_number, zone

LIMITS = {"global": 5, "workspace": 20}  # AnythingLLM's caps (its Memory model)
MAX_TEXT = 500  # a memory is one short fact
# Control characters, and the invisible ones that would make a memory read differently in
# AnythingLLM's Personalization page than in the prompt: C1, bidi marks, overrides and
# isolates, zero-width spaces and the BOM (not ZWJ/ZWNJ, which emoji and scripts need).
HIDDEN = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200b\u200e\u200f\u202a-\u202e\u2060-\u2069\ufeff]"
)


def scope_of(memory: dict) -> str:
    return "global" if memory.get("scope") == "global" else "workspace"


def one_line(text: Any) -> str:
    """A memory's text on one line (the UI's and AnythingLLM's own may have line breaks)."""
    return " ".join(str(text or "").split())


@dataclass
class SavedMemories:
    client: InternalAPI
    timezone: str = DEFAULT_TIMEZONE

    def describe(self, memory: dict, tz: ZoneInfo) -> str:
        used = local(when(memory.get("lastUsedAt")), tz)
        return (
            f"{memory.get('id')} ({scope_of(memory)}, last used {used}): "
            f"{one_line(memory.get('content'))}"
        )

    async def found(self, slug: str) -> dict[str, list[dict]]:
        try:
            return await self.client.memories(slug)
        except NotFound:
            raise RunnerError(
                f"AnythingLLM has no workspace '{slug}', so it keeps no memories for it"
            ) from None

    async def listing(self, slug: str) -> str:
        found = await self.found(slug)
        if not found["global"] and not found["workspace"]:
            return (
                "No saved memories, global or for this workspace. Save one with action save "
                "when the user asks you to remember something."
            )
        lines, tz = [], zone(self.timezone)
        for scope, label in (("global", "Global"), ("workspace", "This workspace's")):
            room = LIMITS[scope] - len(found[scope])
            lines.append(
                f"{label} ({len(found[scope])} of {LIMITS[scope]}, {room} free):"
            )
            lines += [f"- {self.describe(m, tz)}" for m in found[scope]] or ["- none"]
        return "\n".join(lines)

    async def save(self, slug: str, text: Any, scope: Any) -> str:
        if text is not None and not isinstance(text, str):
            raise RunnerError("give the text to remember as text: one short fact")
        text = one_line(text)
        scope = str(scope or "workspace").strip().lower()
        if scope not in LIMITS:
            raise RunnerError("scope must be workspace (the default) or global")
        if not text:
            raise RunnerError(
                "give the text to remember: one short fact about the user"
            )
        if HIDDEN.search(text):
            raise RunnerError(
                "the text can't have control characters or invisible ones (bidi marks, "
                "zero-width spaces)"
            )
        if len(text) > MAX_TEXT:
            raise RunnerError(
                f"a memory is one short fact, at most {MAX_TEXT} characters; this is "
                f"{len(text)}. Shorten it, or save two."
            )
        # A global one is in every chat already; the same text twice only takes a slot.
        found = await self.found(slug)
        same = next(
            (
                m
                for m in found[scope] + (found["global"] if scope != "global" else [])
                if one_line(m.get("content")).casefold() == text.casefold()
            ),
            None,
        )
        if same is not None:
            return (
                f"Memory {same.get('id')} ({scope_of(same)}) already says that: {text}"
            )
        memory = await self.client.memory_new(slug, text, scope)
        where = (
            "every workspace's chats" if scope == "global" else "this workspace's chats"
        )
        return f"Saved memory {memory.get('id')} for {where}: {text}"

    async def forget(self, slug: str, memory_id: Any) -> str:
        memory_id = whole_number(memory_id)
        if memory_id is None:
            raise RunnerError(
                "give the id of the memory to forget (action list shows them)"
            )
        found = await self.found(slug)
        memory = {m.get("id"): m for m in found["global"] + found["workspace"]}.get(
            memory_id
        )
        if memory is None:
            raise RunnerError(
                f"no saved memory {memory_id} for this workspace or global (action list "
                "shows them)"
            )
        await self.client.memory_delete(memory_id)
        return (
            f"Forgot memory {memory_id} ({scope_of(memory)}): "
            f"{one_line(memory.get('content'))}\n"
            "If that was a mistake, save it again with this text and scope."
        )

    async def act(
        self,
        slug: str,
        action: str,
        text: Any = None,
        scope: Any = None,
        memory_id: Any = None,
    ) -> str:
        action = action or "list"
        if action == "list":
            return await self.listing(slug)
        if action == "save":
            return await self.save(slug, text, scope)
        if action == "forget":
            return await self.forget(slug, memory_id)
        raise RunnerError("action must be list, save or forget")
