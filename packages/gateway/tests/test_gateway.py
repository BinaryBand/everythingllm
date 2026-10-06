import asyncio
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import audit.server
import hostrpc
import podcasts.server
import pytest
import sites.server
from gateway import app, grants
from gateway.app import Config, create_app
from starlette.testclient import TestClient

TOKEN = "s3cret-token"
BASE = "http://127.0.0.1:8452"
MCP_HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json, text/event-stream",
}
OTHER = {**MCP_HEADERS, "Authorization": "Bearer another-token"}


@pytest.fixture
def client():
    # claude-code's grant is grants.toml's; other has a token but no grant.
    config = Config(clients={"claude-code": TOKEN, "other": "another-token"})
    with TestClient(create_app(config), base_url=BASE) as c:
        yield c


def post(client, method, params=None, headers=MCP_HEADERS):
    r = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert r.status_code == 200, r.text
    [data] = [line[5:] for line in r.text.splitlines() if line.startswith("data:")]
    return json.loads(data)


def rpc(client, method, params=None, headers=MCP_HEADERS):
    reply = post(client, method, params, headers)
    assert "result" in reply, reply
    return reply


def tool_names(client, headers=MCP_HEADERS):
    return {
        t["name"] for t in rpc(client, "tools/list", headers=headers)["result"]["tools"]
    }


def text_of(reply):
    [content] = reply["result"]["content"]
    return content["text"]


def test_health_needs_no_token_and_everything_else_does(client):
    assert client.get("/health").json() == {"ok": True}
    assert client.post("/mcp", json={}).status_code == 401
    wrong = {**MCP_HEADERS, "Authorization": "Bearer nope"}
    assert client.post("/mcp", headers=wrong, json={}).status_code == 401


def test_every_client_has_its_own_token(client):
    assert "tools" in rpc(client, "tools/list", headers=OTHER)["result"]


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


FRONTS = (sites.server, podcasts.server, audit.server)
READS = {fn.__name__ for front in FRONTS for fn in front.tool.registered}
WRITES = {fn.__name__ for front in FRONTS for fn in front.skills}
AGENTS = {"agents_delegate", "agents_wait", "agents_runs", "agents_cancel"}


def test_claude_code_has_the_fronts_tools_their_skills_and_the_agents_ops(client):
    assert {"list_sites", "list_podcasts", "run_checks"} <= READS
    assert {"write_entry", "delete_entry", "add_podcast", "publish_report"} <= WRITES
    names = tool_names(client)
    assert names == READS | WRITES | AGENTS
    assert not {"delegate", "wait", "runs", "cancel"} & names  # prefixed instead


def test_the_groups_are_the_fronts_reads_and_skills_and_the_gateways_own():
    groups = app.tool_groups()
    assert set(groups) == {
        "sites",
        "sites:write",
        "podcasts",
        "podcasts:write",
        "audit",
        "audit:write",
        "agents",
        "research",
        "sandbox",
    }
    assert set(groups["sites"]) == {f.__name__ for f in sites.server.tool.registered}
    assert set(groups["sites:write"]) == {"write_entry", "delete_entry"}
    assert set(groups["agents"]) == AGENTS
    assert groups["research"] == groups["sandbox"] == {}  # their tools come later


def test_the_repos_grants_give_claude_code_every_group():
    groups = app.tool_groups()
    assert grants.load(groups) == {"claude-code": frozenset(groups)}


def test_a_client_sees_only_the_groups_its_granted():
    config = Config(clients={"reader": TOKEN})
    granted = {"reader": ["sites", "agents"]}
    with TestClient(create_app(config, granted), base_url=BASE) as c:
        names = tool_names(c)
    assert names == {f.__name__ for f in sites.server.tool.registered} | AGENTS


def test_a_client_with_a_token_but_no_grant_sees_nothing(client):
    assert tool_names(client, OTHER) == set()


def refusal(client, name, headers=MCP_HEADERS):
    params = {"name": name, "arguments": {"site": "secret-site-name"}}
    return post(client, "tools/call", params, headers)["error"]


def test_a_call_outside_the_grant_is_refused_and_logged(caplog):
    caplog.set_level("INFO", logger="gateway")
    config = Config(clients={"reader": TOKEN, "other": "another-token"})
    with TestClient(create_app(config, {"reader": ["sites"]}), base_url=BASE) as c:
        error = refusal(c, "write_entry")
        assert error["code"] == -32602
        assert (
            "'write_entry' isn't one this gateway client (reader) may call"
            in error["message"]
        )
        assert "(other)" in refusal(c, "list_sites", OTHER)["message"]
        assert "(reader)" in refusal(c, "no_such_tool")["message"]
    assert "reader was refused 'write_entry'" in caplog.text
    assert "other was refused 'list_sites'" in caplog.text
    assert "secret-site-name" not in caplog.text


class FakeSites(hostrpc.Service):
    async def op_list_entries(self, site, section, limit):
        return f"entries of {site}/{section or 'all'} ({limit})"

    async def op_write_entry(
        self, site, section, slug, title, date, extra, body, overwrite
    ):
        return f"wrote {site}/{section}/{slug}: {title} ({date}, {extra}, {overwrite})"


class FakeAgents(hostrpc.Service):
    async def op_runs(self):
        return {"runs": [{"run_id": "dg-1"}]}


@contextmanager
def fake_runner(monkeypatch, env, service):
    # AF_UNIX paths are short, so not tmp_path.
    sock = Path("/tmp") / f"gateway-test-{os.getpid()}-{env.lower()}.sock"
    monkeypatch.setenv(env, str(sock))
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()
    thread = threading.Thread(
        target=loop.run_until_complete,
        args=(hostrpc.serve(service, sock, stop=stop),),
    )
    thread.start()
    for _ in range(200):
        if sock.exists():
            break
        threading.Event().wait(0.01)
    try:
        yield
    finally:
        loop.call_soon_threadsafe(stop.set)
        thread.join(5)
        sock.unlink(missing_ok=True)


@pytest.fixture
def sites_runner(monkeypatch):
    with fake_runner(monkeypatch, "SITES_SOCKET", FakeSites()):
        yield


def test_a_call_goes_to_the_runner_and_the_log_names_only_client_and_tool(
    client, sites_runner, caplog
):
    caplog.set_level("INFO", logger="gateway")
    reply = rpc(
        client,
        "tools/call",
        {"name": "list_entries", "arguments": {"site": "secret-site-name"}},
    )
    assert text_of(reply) == "entries of secret-site-name/all (20)"
    assert "claude-code called list_entries" in caplog.text
    assert "secret-site-name" not in caplog.text
    assert TOKEN not in caplog.text


def test_a_skill_is_a_tool_forwarded_to_its_runner(client, sites_runner, caplog):
    caplog.set_level("INFO", logger="gateway")
    arguments = {
        "site": "news",
        "section": "notes",
        "slug": "a-note",
        "title": "Secret title",
        "date": "2026-10-06",
    }
    reply = rpc(client, "tools/call", {"name": "write_entry", "arguments": arguments})
    assert text_of(reply) == (
        "wrote news/notes/a-note: Secret title (2026-10-06, None, False)"
    )
    assert "claude-code called write_entry" in caplog.text
    assert "Secret title" not in caplog.text


def test_a_prefixed_tool_sends_the_ops_own_name(client, monkeypatch):
    with fake_runner(monkeypatch, "AGENTS_SOCKET", FakeAgents()):
        reply = rpc(client, "tools/call", {"name": "agents_runs", "arguments": {}})
    assert json.loads(text_of(reply)) == {"runs": [{"run_id": "dg-1"}]}


async def whoami() -> str:
    """Who is calling."""
    return grants.client.get() or "nobody"


def test_a_tool_knows_which_client_called_it(monkeypatch):
    probe = SimpleNamespace(
        __name__="probe",
        skills=hostrpc.Skills("probe", "PROBE_SOCKET"),
        tool=SimpleNamespace(registered=[whoami]),
    )
    monkeypatch.setattr(app, "FRONTS", (*app.FRONTS, probe))
    config = Config(clients={"claude-code": TOKEN, "other": "another-token"})
    granted = {"claude-code": ["probe"], "other": ["probe"]}
    with TestClient(create_app(config, granted), base_url=BASE) as c:
        call = {"name": "whoami", "arguments": {}}
        assert text_of(rpc(c, "tools/call", call)) == "claude-code"
        assert text_of(rpc(c, "tools/call", call, headers=OTHER)) == "other"
    assert grants.client.get() is None


def test_two_fronts_with_one_tool_name_dont_start(monkeypatch):
    twin = SimpleNamespace(
        __name__="twin",
        skills=hostrpc.Skills("twin", "TWIN_SOCKET"),
        tool=SimpleNamespace(registered=sites.server.tool.registered),
    )
    monkeypatch.setattr(app, "FRONTS", (sites.server, twin))
    with pytest.raises(RuntimeError, match="both have a tool list_sites"):
        app.tool_groups()


def test_a_grant_of_a_group_there_isnt_doesnt_start():
    with pytest.raises(ValueError, match=r"unknown group\(s\) \['sitez'\]"):
        create_app(Config(clients={"c": TOKEN}), {"c": ["sitez"]})


def grants_file(tmp_path, text):
    path = tmp_path / "grants.toml"
    path.write_text(text)
    return path


def test_grants_name_each_clients_groups(tmp_path):
    path = grants_file(
        tmp_path,
        '[clients.laptop]\ntools = ["sites", "agents"]\n[clients.none]\ntools = []\n',
    )
    assert grants.load(["sites", "agents", "audit"], path) == {
        "laptop": frozenset({"sites", "agents"}),
        "none": frozenset(),
    }


@pytest.mark.parametrize(
    "text, error",
    [
        ('[clients.laptop]\ntools = ["sitez"]\n', r"unknown group\(s\) \['sitez'\]"),
        ('[clients.laptop]\ntool = ["sites"]\n', r"unknown field\(s\) \['tool'\]"),
        ('[client.laptop]\ntools = ["sites"]\n', r"unknown key\(s\) \['client'\]"),
        ('[clients.laptop]\ntools = "sites"\n', "must be a list"),
        ("clients = { laptop = 1 }\n", "not a table"),
    ],
)
def test_grants_refuse_what_they_dont_know(tmp_path, text, error):
    with pytest.raises(ValueError, match=error):
        grants.load(["sites"], grants_file(tmp_path, text))


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
