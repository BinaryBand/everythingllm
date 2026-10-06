import asyncio
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import audit.server
import hostrpc
import podcasts.server
import pytest
import sites.server
from gateway import agents, app
from gateway.app import Config, create_app
from starlette.testclient import TestClient

TOKEN = "s3cret-token"
BASE = "http://127.0.0.1:8452"
MCP_HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json, text/event-stream",
}


@pytest.fixture
def client():
    config = Config(clients={"claude-code": TOKEN, "other": "another-token"})
    with TestClient(create_app(config), base_url=BASE) as c:
        yield c


def rpc(client, method, params=None, headers=MCP_HEADERS):
    r = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert r.status_code == 200, r.text
    [data] = [line[5:] for line in r.text.splitlines() if line.startswith("data:")]
    return json.loads(data)


def test_health_needs_no_token_and_everything_else_does(client):
    assert client.get("/health").json() == {"ok": True}
    assert client.post("/mcp", json={}).status_code == 401
    wrong = {**MCP_HEADERS, "Authorization": "Bearer nope"}
    assert client.post("/mcp", headers=wrong, json={}).status_code == 401


def test_every_client_has_its_own_token(client):
    other = {**MCP_HEADERS, "Authorization": "Bearer another-token"}
    assert "tools" in rpc(client, "tools/list", headers=other)["result"]


def test_a_host_it_doesnt_know_is_refused():
    with TestClient(
        create_app(Config(clients={"c": TOKEN})), base_url="http://evil.example"
    ) as c:
        r = c.post("/mcp", headers=MCP_HEADERS, json={})
        assert r.status_code in (400, 421)


def test_the_public_host_is_allowed():
    config = Config(clients={"c": TOKEN}, public_host="box.tail.ts.net")
    with TestClient(create_app(config), base_url="https://box.tail.ts.net:8452") as c:
        assert "tools" in rpc(c, "tools/list")["result"]


def test_the_tools_are_the_fronts_tools_and_the_agents_ops(client):
    names = {t["name"] for t in rpc(client, "tools/list")["result"]["tools"]}
    fronts = {
        fn.__name__
        for front in (sites.server, podcasts.server, audit.server, agents)
        for fn in front.tool.registered
    }
    assert names == fronts
    assert {"list_sites", "list_podcasts", "run_checks"} <= names
    assert {"delegate", "wait", "runs", "cancel"} <= names
    skills = {
        fn.__name__
        for front in (sites.server, podcasts.server, audit.server)
        for fn in front.skills
    }
    assert skills and not skills & names  # the ops that write or act stay skills


def test_two_fronts_with_one_tool_name_dont_start(monkeypatch):
    twin = SimpleNamespace(
        __name__="twin", tool=SimpleNamespace(registered=sites.server.tool.registered)
    )
    monkeypatch.setattr(app, "FRONTS", (sites.server, twin))
    with pytest.raises(RuntimeError, match="both have a tool list_sites"):
        app.build_mcp()


class FakeSites(hostrpc.Service):
    async def op_list_entries(self, site, section, limit):
        return f"entries of {site}/{section or 'all'} ({limit})"


@pytest.fixture
def sites_runner(monkeypatch):
    sock = Path("/tmp") / f"gateway-test-{os.getpid()}.sock"  # AF_UNIX paths are short
    monkeypatch.setenv("SITES_SOCKET", str(sock))
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()
    thread = threading.Thread(
        target=loop.run_until_complete,
        args=(hostrpc.serve(FakeSites(), sock, stop=stop),),
    )
    thread.start()
    for _ in range(200):
        if sock.exists():
            break
        threading.Event().wait(0.01)
    yield
    loop.call_soon_threadsafe(stop.set)
    thread.join(5)
    sock.unlink(missing_ok=True)


def test_a_call_goes_to_the_runner_and_the_log_names_only_client_and_tool(
    client, sites_runner, caplog
):
    caplog.set_level("INFO", logger="gateway")
    reply = rpc(
        client,
        "tools/call",
        {"name": "list_entries", "arguments": {"site": "secret-site-name"}},
    )
    [content] = reply["result"]["content"]
    assert content["text"] == "entries of secret-site-name/all (20)"
    assert "claude-code called list_entries" in caplog.text
    assert "secret-site-name" not in caplog.text
    assert TOKEN not in caplog.text


def test_host_sockets_points_the_fronts_at_the_hosts_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("ANYTHINGLLM_STORAGE", str(tmp_path))
    for front in app.FRONTS:
        monkeypatch.delenv(front.skills.env, raising=False)
    monkeypatch.setenv("AUDIT_SOCKET", "/elsewhere.sock")
    app.host_sockets()
    assert os.environ["SITES_SOCKET"] == str(
        tmp_path / "everythingllm/sites/runner.sock"
    )
    assert os.environ["AGENTS_SOCKET"] == str(
        tmp_path / "everythingllm/agents/runner.sock"
    )
    assert os.environ["AUDIT_SOCKET"] == "/elsewhere.sock"  # a set one wins


def test_config_names_a_client_per_token(monkeypatch):
    monkeypatch.setenv("GATEWAY_TOKEN_CLAUDE_CODE", "a")
    monkeypatch.setenv("GATEWAY_TOKEN_LAPTOP", "b")
    monkeypatch.setenv("GATEWAY_TOKEN_EMPTY", "")
    assert Config.from_env().clients == {"claude-code": "a", "laptop": "b"}


def test_no_token_no_gateway(monkeypatch):
    for k in list(os.environ):
        if k.startswith("GATEWAY_TOKEN_"):
            monkeypatch.delenv(k)
    with pytest.raises(SystemExit, match="GATEWAY_TOKEN_"):
        Config.from_env()
