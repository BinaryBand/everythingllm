"""agents-runner's side of AnythingLLM's saved memories, for the memories skill: list a
chat's (global and its workspace's), save one, or forget one, each at once: forgetting gives
back the text, so a mistake is undone by saving it again. The README's "Saved memories" has
the rules.

Config (environment, from host.env through the unit):
  USER_TIMEZONE  the user's time zone, for the "last used" times `list` shows
"""

from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from hostrpc import RunnerError

from agents.anythingllm import InternalAPI
from agents.jobs import CONTROL, DEFAULT_TIMEZONE, local, when, zone

LIMITS = {"global": 5, "workspace": 20}  # AnythingLLM's caps (its Memory model)
MAX_TEXT = 500  # a memory is one short fact


def scope_of(memory: dict) -> str:
    return "global" if memory.get("scope") == "global" else "workspace"


@dataclass
class SavedMemories:
    client: InternalAPI
    timezone: str = DEFAULT_TIMEZONE

    def describe(self, memory: dict, tz: ZoneInfo) -> str:
        used = local(when(memory.get("lastUsedAt")), tz)
        return (
            f"{memory.get('id')} ({scope_of(memory)}, last used {used}): "
            f"{memory.get('content', '')}"
        )

    async def listing(self, slug: str) -> str:
        found = await self.client.memories(slug)
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
        text = " ".join((text or "").split())
        scope = str(scope or "workspace").strip().lower()
        if scope not in LIMITS:
            raise RunnerError("scope must be workspace (the default) or global")
        if not text:
            raise RunnerError(
                "give the text to remember: one short fact about the user"
            )
        if CONTROL.search(text):
            raise RunnerError("the text can't have control characters")
        if len(text) > MAX_TEXT:
            raise RunnerError(
                f"a memory is one short fact, at most {MAX_TEXT} characters; this is "
                f"{len(text)}. Shorten it, or save two."
            )
        memory = await self.client.memory_new(slug, text, scope)
        where = (
            "every workspace's chats" if scope == "global" else "this workspace's chats"
        )
        return f"Saved memory {memory.get('id')} for {where}: {text}"

    async def forget(self, slug: str, memory_id: Any) -> str:
        if isinstance(memory_id, bool) or (
            isinstance(memory_id, float) and not memory_id.is_integer()
        ):
            memory_id = None  # int() would make true memory 1, and 12.7 memory 12
        try:
            memory_id = int(memory_id)
        except (TypeError, ValueError):
            raise RunnerError(
                "give the id of the memory to forget (action list shows them)"
            ) from None
        found = await self.client.memories(slug)
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
            f"Forgot memory {memory_id} ({scope_of(memory)}): {memory.get('content', '')}\n"
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
