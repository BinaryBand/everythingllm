"""The long-term memory's client route: a client with the key reads a workspace's facts and
files, recalls, remembers and forgets, and nothing without the key."""

import asyncio
import json

import pytest
from hostenv.keys import REFUSED
from memory import clientweb, runner
from memory.store import Store
from test_memory import Words, chat, done, sandbox

KEY = "good-key"


@pytest.fixture
def memory(tmp_path):
    """test_memory's service fixture, under a name of its own."""
    store = Store(tmp_path / "memory" / "memory.db")
    model = runner.Model("words", Words, lambda m: store.reembed(m.name, m.embed))
    model.start()
    s = runner.MemoryService(store, model, tmp_path / "workspaces")
    s.ingester.start()
    return s


async def check(key: str):
    return None if key == KEY else (403, REFUSED)


def call(service, method, path, body=None, key=KEY, headers=None):
    """One request to a ClientWeb over `service`: its status and JSON body (or None)."""

    async def scenario():
        server = await clientweb.ClientWeb(service, check).serve(0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        data = json.dumps(body).encode() if body is not None else b""
        lines = [f"{method} {path} HTTP/1.1", "Host: localhost"]
        if key:
            lines.append(f"Authorization: Bearer {key}")
        if body is not None:
            lines += ["Content-Type: application/json", f"Content-Length: {len(data)}"]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + data)
        await writer.drain()
        raw = await reader.read()
        writer.close()
        server.close()
        return raw

    raw = asyncio.run(scenario())
    head, _, payload = raw.partition(b"\r\n\r\n")
    status = int(head.split()[1])
    try:
        return status, json.loads(payload)
    except ValueError:
        return status, None


def test_a_key_is_needed_and_a_wrong_one_refused(memory):
    assert call(memory, "GET", "/_live/memory/home", key=None) == (
        401,
        {"error": REFUSED},
    )
    assert call(memory, "GET", "/_live/memory/home", key="wrong")[0] == 403


def test_the_listing_is_what_the_workspaces_chats_see(memory):
    memory.op_remember(chat("home"), "The heat pump is a Nibe F2120.")
    memory.op_remember(chat("home"), "Prefers trains to flights.", "global")
    memory.op_remember(chat("career"), "Applies for jobs in Malmö.")
    status, body = call(memory, "GET", "/_live/memory/home")
    assert status == 200
    assert body["workspace"] == "home" and body["count"] == 2
    assert [(f["scope"], f["text"]) for f in body["facts"]] == [
        ("global", "Prefers trains to flights."),
        ("workspace", "The heat pump is a Nibe F2120."),
    ]
    assert all(f["id"].startswith("m") and f["created"] for f in body["facts"])
    assert body["files"] == body["waiting"] == body["failed"] == []
    # The machine's route may strip the prefix.
    assert call(memory, "GET", "/home")[1]["count"] == 2


def test_ingested_files_are_listed_and_their_text_forgotten_by_path(memory, tmp_path):
    folder = sandbox(tmp_path) / "project"
    (folder / "boiler.md").write_text("Service the boiler every October.\n")
    memory.op_ingest(chat("home"), ["/project/boiler.md"])
    done(memory)
    files = call(memory, "GET", "/_live/memory/home")[1]["files"]
    assert [(f["path"], f["parts"]) for f in files] == [("/project/boiler.md", 1)]
    gone = call(memory, "POST", "/_live/memory/home/forget?path=/project/boiler.md")
    assert gone == (200, {"dropped": 1})
    assert call(memory, "GET", "/_live/memory/home")[1]["files"] == []
    assert (
        call(memory, "POST", "/_live/memory/home/forget?path=/project/x.md")[0] == 404
    )
    assert (folder / "boiler.md").exists()


def test_recall_answers_closest_first_with_ids_a_client_can_forget_by(memory):
    memory.op_remember(chat("home"), "The heat pump is a Nibe F2120.")
    memory.op_remember(chat("home"), "Prefers trains to flights.", "global")
    status, body = call(
        memory, "GET", "/_live/memory/home/recall?q=which+heat+pump&k=1"
    )
    assert status == 200
    [hit] = body["hits"]
    assert hit["text"] == "The heat pump is a Nibe F2120."
    assert hit["scope"] == "workspace" and hit["id"].startswith("m")
    assert call(memory, "GET", "/_live/memory/home/recall?q=")[0] == 400
    assert call(memory, "GET", "/_live/memory/home/recall?q=pump&k=99")[0] == 400


def test_recall_says_when_the_model_cant_answer_yet(tmp_path):
    def broken():
        raise OSError("no model here")

    store = Store(tmp_path / "memory.db")
    model = runner.Model("broken", broken)
    status, body = call(
        runner.MemoryService(store, model, tmp_path), "GET", "/home/recall?q=pump"
    )
    assert status == 503 and "couldn't load" in body["error"]


def test_remember_keeps_a_fact_once_in_the_scope_asked(memory):
    status, body = call(
        memory,
        "POST",
        "/_live/memory/home/remember",
        {"text": "Takes coffee black.", "scope": "global"},
    )
    assert status == 200 and body["new"] is True
    assert body["fact"]["scope"] == "global"
    again = call(
        memory, "POST", "/_live/memory/home/remember", {"text": "takes coffee  black."}
    )[1]
    assert again["new"] is False and again["fact"]["id"] == body["fact"]["id"]
    assert call(memory, "GET", "/_live/memory/career")[1]["count"] == 1


def test_remember_refuses_what_isnt_a_short_plain_fact(memory):
    path = "/_live/memory/home/remember"
    assert call(memory, "POST", path, {"text": "Lives in\u202eStockholm"})[0] == 400
    assert call(memory, "POST", path, {"text": ""})[0] == 400
    assert call(memory, "POST", path, {"text": "Hi.", "scope": "thread"})[0] == 400
    assert call(memory, "POST", path, ["not", "an", "object"])[0] == 400
    assert call(memory, "POST", path, {"text": "x" * 9000})[0] == 413


def test_remember_says_when_a_scope_is_full(memory, monkeypatch):
    monkeypatch.setattr(runner, "MAX_FACTS", 1)
    path = "/_live/memory/home/remember"
    assert call(memory, "POST", path, {"text": "Lives in Uppsala."})[0] == 200
    assert call(memory, "POST", path, {"text": "Takes coffee black."})[0] == 409


def test_forget_takes_a_fact_this_workspace_sees_and_no_other(memory):
    home = memory.op_remember(chat("home"), "The heat pump is a Nibe F2120.")["id"]
    away = memory.op_remember(chat("career"), "Applies for jobs in Malmö.")["id"]
    assert call(memory, "POST", f"/_live/memory/home/forget?id=m{away}")[0] == 404
    assert call(memory, "POST", f"/_live/memory/home/forget?id=m{home}") == (
        200,
        {"forgotten": f"m{home}"},
    )
    assert call(memory, "GET", "/_live/memory/home")[1]["count"] == 0
    assert call(memory, "POST", "/_live/memory/home/forget")[0] == 400


def test_methods_preflight_and_names_that_arent_workspaces(memory):
    assert call(memory, "GET", "/_live/memory/home/remember")[0] == 405
    assert call(memory, "POST", "/_live/memory/home")[0] == 405
    status, _ = call(memory, "OPTIONS", "/_live/memory/home/remember", key=None)
    assert status == 204
    for name in ("_jobs", "_host", "a/b", ""):
        assert call(memory, "GET", f"/_live/memory/{name}")[0] == 404


def test_the_preflight_lets_a_web_client_send_json(memory):
    async def headers():
        server = await clientweb.ClientWeb(memory, check).serve(0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"OPTIONS /_live/memory/home/remember HTTP/1.1\r\n\r\n")
        raw = await reader.read()
        writer.close()
        server.close()
        return raw.decode()

    raw = asyncio.run(headers())
    assert "Access-Control-Allow-Origin: *" in raw
    assert "Access-Control-Allow-Headers: Authorization, Content-Type" in raw


@pytest.mark.parametrize("prefix", ["/_live/memory", ""])
def test_a_route_is_the_same_with_or_without_the_prefix(memory, prefix):
    assert call(memory, "GET", f"{prefix}/home")[0] == 200


def test_the_client_route_runs_beside_the_socket(memory, short_sock, monkeypatch):
    monkeypatch.setenv("MEMORY_SOCKET", str(short_sock))

    async def scenario():
        task = asyncio.create_task(runner.run(memory, 0))
        for _ in range(500):
            if short_sock.exists():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        return short_sock.exists()

    assert asyncio.run(scenario())
