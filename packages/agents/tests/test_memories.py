import asyncio
import json

import httpx
import pytest
from agents import runner
from agents.anythingllm import AnythingLLMError, InternalAPI, internal_error
from agents.memories import LIMITS
from hostrpc import RunnerError

CHAT = {"workspace": "career", "thread": "default"}


class FakeMemoriesAPI:
    """AnythingLLM's internal API, as much of its saved memories as agents-runner uses: each
    workspace's own, the global ones, its caps, and its 403 while they're turned off."""

    def __init__(self):
        self.memories: dict[int, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.enabled = True
        self.next_id = 1

    def add(self, content, workspace="career", scope="workspace", used=None):
        self.memories[self.next_id] = {
            "id": self.next_id,
            "userId": None,
            "workspaceId": None if scope == "global" else workspace,
            "scope": scope,
            "content": content,
            "lastUsedAt": used,
        }
        self.next_id += 1
        return self.next_id - 1

    def of(self, slug):
        newest = sorted(self.memories.values(), key=lambda m: -m["id"])
        return {
            "global": [m for m in newest if m["scope"] == "global"],
            "workspace": [
                m
                for m in newest
                if m["scope"] == "workspace" and m["workspaceId"] == slug
            ],
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        self.calls.append((request.method, path))
        if not self.enabled:
            return httpx.Response(403, json={"error": "Personalization is disabled."})
        parts = path.strip("/").split("/")
        if parts[0] == "workspaces" and request.method == "GET":
            return httpx.Response(200, json={"memories": self.of(parts[1])})
        if parts[0] == "workspaces" and request.method == "POST":
            body = json.loads(request.content)
            scope = body.get("scope", "workspace")
            if len(self.of(parts[1])[scope]) >= LIMITS[scope]:
                limit = LIMITS[scope]
                error = f"Maximum {scope} memory limit ({limit}) reached."
                return httpx.Response(400, json={"error": error})
            memory_id = self.add(body["content"].strip(), parts[1], scope)
            return httpx.Response(200, json={"memory": self.memories[memory_id]})
        if parts[0] == "memories" and request.method == "DELETE":
            if self.memories.pop(int(parts[1]), None) is None:
                return httpx.Response(404, json={"error": "Memory not found."})
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404)


@pytest.fixture
def api():
    return FakeMemoriesAPI()


def make(api, tmp_path):
    internal = InternalAPI(
        "http://allm",
        tmp_path / ".env",
        transport=httpx.MockTransport(api),
        login=lambda fresh: {"Authorization": "Bearer good"},
    )
    return runner.Runner(runner.Settings(runlogs=tmp_path / "runs"), internal=internal)


def test_list_shows_the_global_ones_and_only_this_workspaces_with_room_left(
    api, tmp_path
):
    api.add("Lives in Stockholm.", scope="global", used="2026-10-07T12:05:00.000Z")
    api.add("Is applying for backend roles.")
    api.add("Is learning Rust.", workspace="education")

    async def main():
        r = make(api, tmp_path)
        text = await r.op_memories(CHAT)
        assert "Global (1 of 5, 4 free):" in text
        assert (
            "1 (global, last used Wed 2026-10-07 14:05 CEST): Lives in Stockholm."
            in text
        )
        assert "This workspace's (1 of 20, 19 free):" in text
        assert "2 (workspace, last used never): Is applying for backend roles." in text
        assert "Rust" not in text
        assert await r.op_memories({"workspace": "cloud"}, "list") == (
            "Global (1 of 5, 4 free):\n"
            "- 1 (global, last used Wed 2026-10-07 14:05 CEST): Lives in Stockholm.\n"
            "This workspace's (0 of 20, 20 free):\n- none"
        )
        api.memories.clear()
        assert (await r.op_memories(CHAT)).startswith("No saved memories")
        assert (await r.op_memories(CHAT, "")).startswith("No saved memories")

    asyncio.run(main())


def test_save_keeps_one_short_fact_in_the_scope_asked_for(api, tmp_path):
    async def main():
        r = make(api, tmp_path)
        text = await r.op_memories(CHAT, "save", "  Prefers  metric\nunits. ")
        assert (
            text == "Saved memory 1 for this workspace's chats: Prefers metric units."
        )
        text = await r.op_memories(CHAT, "save", "Lives in Stockholm.", "Global")
        assert text == "Saved memory 2 for every workspace's chats: Lives in Stockholm."
        assert [(m["scope"], m["workspaceId"]) for m in api.memories.values()] == [
            ("workspace", "career"),
            ("global", None),
        ]
        for text, scope, error in [
            ("", None, "give the text to remember"),
            ("x" * 501, None, "at most 500 characters"),
            ("a\x00b", None, "control characters"),
            (["Lives in Stockholm.", "Likes tea."], None, "as text"),
            ("fine", "thread", "scope must be workspace"),
        ]:
            with pytest.raises(RunnerError, match=error):
                await r.op_memories(CHAT, "save", text, scope)
        for n in range(4):
            api.add(f"global fact {n}", scope="global")
        with pytest.raises(
            AnythingLLMError, match=r"global memory limit \(5\) reached"
        ):
            await r.op_memories(CHAT, "save", "one too many", "global")
        assert len(api.memories) == 6

    asyncio.run(main())


def test_forget_deletes_at_once_gives_the_text_back_and_only_this_workspaces(
    api, tmp_path
):
    mine = api.add("Is applying for backend roles.")
    theirs = api.add("Is learning Rust.", workspace="education")

    async def main():
        r = make(api, tmp_path)
        text = await r.op_memories(CHAT, "forget", memory_id=str(mine))
        assert text.startswith(
            f"Forgot memory {mine} (workspace): Is applying for backend roles.\n"
        )
        assert "save it again" in text
        assert mine not in api.memories
        for memory_id, error in [
            (theirs, "no saved memory 2 for this workspace or global"),
            (None, "give the id of the memory to forget"),
            (True, "give the id of the memory to forget"),
            (theirs + 0.5, "give the id of the memory to forget"),
        ]:
            with pytest.raises(RunnerError, match=error):
                await r.op_memories(CHAT, "forget", memory_id=memory_id)
        assert theirs in api.memories
        assert ("DELETE", f"/memories/{theirs}") not in api.calls

    asyncio.run(main())


def test_memories_are_a_chats_and_say_when_theyre_turned_off(api, tmp_path):
    async def main():
        r = make(api, tmp_path)
        # chat_only's cases are test_scheduled_jobs'; this one shows the op asks it.
        with pytest.raises(RunnerError, match="a delegated task can't"):
            await r.op_memories({"workspace": "agents-worker"})
        assert api.calls == []
        with pytest.raises(RunnerError, match="action must be list, save or forget"):
            await r.op_memories(CHAT, "edit")
        api.enabled = False
        with pytest.raises(AnythingLLMError, match="Personalization is disabled"):
            await r.op_memories(CHAT)

    asyncio.run(main())


def test_a_server_error_reads_as_one_even_when_anythingllm_says_why():
    assert internal_error(500, {"error": "db locked"}) == (
        "AnythingLLM hit an error (500): db locked"
    )
    assert internal_error(400, {"error": "too long"}) == (
        "AnythingLLM turned it down: too long"
    )
    assert internal_error(500, None) == "AnythingLLM hit an error (500)."
