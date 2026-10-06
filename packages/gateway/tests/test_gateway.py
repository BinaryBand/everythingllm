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
import research.job
import sandbox.runner
import sites.server
from gateway import app, grants
from gateway import research as gateway_research
from gateway import sandbox as gateway_sandbox
from gateway.app import Config, create_app
from hostctl import gateway_env
from mcp.server.mcpserver.exceptions import ToolError
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
RESEARCH = {"research_start", "research_wait", "research_runs"}
SANDBOX = {
    "sandbox_run",
    "sandbox_wait",
    "sandbox_write",
    "sandbox_publish",
    "sandbox_build_site",
}


def test_claude_code_has_the_fronts_tools_their_skills_and_the_gateways_own(client):
    assert {"list_sites", "list_podcasts", "run_checks"} <= READS
    assert {"write_entry", "delete_entry", "add_podcast", "publish_report"} <= WRITES
    names = tool_names(client)
    assert names == READS | WRITES | AGENTS | RESEARCH | SANDBOX
    # prefixed instead
    assert not {"delegate", "wait", "runs", "cancel", "start", "run"} & names


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
    assert set(groups["research"]) == RESEARCH
    assert set(groups["sandbox"]) == SANDBOX


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


def test_config_refuses_a_client_name_the_sandbox_cant_take(monkeypatch):
    for k in list(os.environ):
        if k.startswith("GATEWAY_TOKEN_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("GATEWAY_TOKEN_CLAUDE_CODE", "a")
    monkeypatch.setenv("GATEWAY_TOKEN_BAD.NAME", "b")
    monkeypatch.setenv("GATEWAY_TOKEN__EDGE", "c")
    with pytest.raises(SystemExit, match=r"\['-edge', 'bad\.name'\]"):
        Config.from_env()


def test_gateway_client_makes_the_names_and_keys_the_gateway_reads(monkeypatch):
    assert gateway_env.NAME_RE.pattern == app.CLIENT_RE.pattern
    for k in list(os.environ):
        if k.startswith("GATEWAY_TOKEN_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv(gateway_env.key("pi-2"), "t")
    assert Config.from_env().clients == {"pi-2": "t"}


# --- the sandbox, in the client's own workspace ---


class FakeSandbox(hostrpc.Service):
    """Records each op with its args; a run is still going for `waits` waits."""

    def __init__(self, waits=0):
        super().__init__()
        self.calls = []
        self.waits = waits

    async def reply(self, msg):
        self.calls.append((msg["op"], msg["args"]))
        return await super().reply(msg)

    async def op_run(self, scope, language, code, timeout):
        return {"run_id": "r-1", "running": True, "seconds": 45.0}

    async def op_build_site(self, scope, path, slug):
        return {"run_id": "r-2", "running": True, "seconds": 45.0}

    async def op_wait(self, scope, run_id):
        if self.waits:
            self.waits -= 1
            return {"run_id": run_id, "running": True, "seconds": 90.0}
        return {"run_id": run_id, "exit_code": 0, "stdout": "hi\n"}

    async def op_write(self, scope, path, content, delete):
        return {"path": path, "bytes": len(content)}

    async def op_publish(self, scope, slug, path, remove):
        return {"site": f"https://ws.example/{scope['workspace']}/", "pages": []}


@contextmanager
def sandbox_runner(monkeypatch, service):
    with fake_runner(monkeypatch, "SANDBOX_SOCKET", service):
        yield service


def call_tool(c, name, arguments, headers=MCP_HEADERS):
    reply = rpc(c, "tools/call", {"name": name, "arguments": arguments}, headers)
    assert not reply["result"].get("isError"), reply
    return json.loads(text_of(reply))


ME = {"workspace": "client-claude-code", "thread": "gateway"}


def test_each_sandbox_tool_sends_the_clients_own_scope(client, monkeypatch):
    with sandbox_runner(monkeypatch, FakeSandbox()) as fake:
        call_tool(client, "sandbox_write", {"path": "/work/a.txt", "content": "hi"})
        call_tool(client, "sandbox_publish", {})
        call_tool(client, "sandbox_wait", {"run_id": "r-9"})
        call_tool(client, "sandbox_run", {"language": "python", "code": "print(1)"})
        call_tool(client, "sandbox_build_site", {"path": "/project/site"})
    assert fake.calls == [
        (
            "write",
            {"path": "/work/a.txt", "content": "hi", "delete": False, "scope": ME},
        ),
        ("publish", {"slug": "", "path": "", "remove": False, "scope": ME}),
        ("wait", {"run_id": "r-9", "scope": ME}),
        (
            "run",
            {"language": "python", "code": "print(1)", "timeout": 60, "scope": ME},
        ),
        ("wait", {"run_id": "r-1", "scope": ME}),
        ("build_site", {"path": "/project/site", "slug": "", "scope": ME}),
        ("wait", {"run_id": "r-2", "scope": ME}),
    ]


def test_the_model_cant_give_a_scope(client, monkeypatch):
    evil = {"workspace": "someone-else", "thread": "default"}
    arguments = {"path": "x", "content": "", "scope": evil}
    with sandbox_runner(monkeypatch, FakeSandbox()) as fake:
        post(client, "tools/call", {"name": "sandbox_write", "arguments": arguments})
    assert fake.calls and all(args["scope"] == ME for _, args in fake.calls)


def test_each_client_has_a_sandbox_workspace_of_its_own(monkeypatch):
    config = Config(clients={"claude-code": TOKEN, "other": "another-token"})
    granted = {"claude-code": ["sandbox"], "other": ["sandbox"]}
    with (
        sandbox_runner(monkeypatch, FakeSandbox()) as fake,
        TestClient(create_app(config, granted), base_url=BASE) as c,
    ):
        mine = call_tool(c, "sandbox_publish", {})
        theirs = call_tool(c, "sandbox_publish", {}, headers=OTHER)
    assert mine["site"] == "https://ws.example/client-claude-code/"
    assert theirs["site"] == "https://ws.example/client-other/"
    assert [args["scope"]["workspace"] for _, args in fake.calls] == [
        "client-claude-code",
        "client-other",
    ]


def test_a_run_still_going_is_waited_on_while_the_budget_lasts(client, monkeypatch):
    with sandbox_runner(monkeypatch, FakeSandbox(waits=2)) as fake:
        result = call_tool(client, "sandbox_run", {"language": "bash", "code": "ls"})
    assert result == {"run_id": "r-1", "exit_code": 0, "stdout": "hi\n"}
    assert [op for op, _ in fake.calls] == ["run", "wait", "wait", "wait"]


def test_a_run_past_the_budget_answers_running(client, monkeypatch):
    # The real runner answers a run after its full WAIT, so another wouldn't fit.
    monkeypatch.setattr(gateway_sandbox, "BUDGET", gateway_sandbox.RUNNER_WAIT - 1)
    with sandbox_runner(monkeypatch, FakeSandbox()) as fake:
        result = call_tool(client, "sandbox_run", {"language": "bash", "code": "ls"})
    assert result == {"run_id": "r-1", "running": True, "seconds": 45.0}
    assert [op for op, _ in fake.calls] == ["run"]


def test_the_waits_fit_the_callers_timeout():
    assert gateway_sandbox.RUNNER_WAIT < gateway_sandbox.BUDGET <= hostrpc.CALL_TIMEOUT


@pytest.mark.parametrize("name", [None, "", "Bad Name", "x" * 94])
def test_a_client_name_that_isnt_a_sandbox_key_gets_no_scope(name):
    token = grants.client.set(name)
    try:
        with pytest.raises(ToolError, match="lowercase letters, digits"):
            gateway_sandbox.scope()
    finally:
        grants.client.reset(token)


def test_the_gateways_copies_of_the_sandboxs_limits_match_it():
    assert gateway_sandbox.KEY_RE.pattern == sandbox.runner.KEY_RE.pattern
    assert gateway_sandbox.RUNNER_WAIT == sandbox.runner.WAIT
    assert gateway_sandbox.LIMIT == sandbox.runner.LIMIT
    # The longest client name the gateway takes still makes a sandbox key.
    longest = "a" * 63
    assert app.CLIENT_RE.fullmatch(longest) and not app.CLIENT_RE.fullmatch(
        longest + "a"
    )
    assert sandbox.runner.KEY_RE.fullmatch(gateway_sandbox.WORKSPACE + longest)


def test_a_write_lands_in_the_clients_folder_on_the_real_runner(
    client, monkeypatch, tmp_path
):
    config = sandbox.runner.Config(
        socket=tmp_path / "unused.sock",
        root=tmp_path / "workspaces",
        system_themes=tmp_path / "themes",
        site_dir=tmp_path / "site",
        site_url="https://pages.example/",
        public_root=tmp_path / "public",
        public_url="https://ws.example/",
    )
    with fake_runner(monkeypatch, "SANDBOX_SOCKET", sandbox.runner.Runner(config)):
        result = call_tool(
            client, "sandbox_write", {"path": "/project/notes.md", "content": "hello"}
        )
        reply = rpc(
            client,
            "tools/call",
            {"name": "sandbox_write", "arguments": {"path": "/shared/other/x"}},
        )
    assert result == {"path": "/project/notes.md", "bytes": 5}
    home = tmp_path / "workspaces" / "client-claude-code"
    assert (home / "project" / "notes.md").read_text() == "hello"
    assert (home / "threads" / "gateway").is_dir()
    assert reply["result"]["isError"]
    assert "other's shared folder, which is read-only" in text_of(reply)


# --- research, without a workspace ---


class FakeResearch(hostrpc.Service):
    def __init__(self):
        super().__init__()
        self.started = []

    async def op_start(self, question, **args):
        self.started.append(research.job.Request.of(question, **args))
        return {"run_id": "dr-1", "queued": 0, "card": ""}

    async def op_wait(self, run_id, since=0):
        return {"events": [f"{run_id} from {since}"], "done": False, "result": None}

    async def op_runs(self):
        return {"runs": [{"run_id": "dr-1", "question": "q", "done": False}]}


def test_research_starts_a_run_with_no_workspace_to_embed_in(client, monkeypatch):
    fake = FakeResearch()
    with fake_runner(monkeypatch, "RESEARCH_SOCKET", fake):
        started = call_tool(
            client,
            "research_start",
            {
                "question": "How do heat pumps fare in Nordic winters?",
                "depth": "quick",
                "sub_questions": ["Field data", {"goal": "Costs", "queries": ["x"]}],
                "title": "Heat pumps up north",
                "workspace": "someone-elses",  # not a parameter: never sent
            },
        )
        waited = call_tool(client, "research_wait", {"run_id": "dr-1", "since": 3})
        runs = call_tool(client, "research_runs", {})
    assert started == {"run_id": "dr-1", "queued": 0, "card": ""}
    [req] = fake.started
    assert req.question == "How do heat pumps fare in Nordic winters?"
    assert (req.depth, req.title) == ("quick", "Heat pumps up north")
    assert req.sub_questions == ["Field data", {"goal": "Costs", "queries": ["x"]}]
    assert req.workspace is None and req.workspace_name is None and not req.embed
    assert req.planner == research.job.Request.planner  # the runner's own defaults
    assert waited["events"] == ["dr-1 from 3"]
    assert runs["runs"][0]["run_id"] == "dr-1"


def test_the_research_tools_say_where_the_report_is_read():
    doc = gateway_research.start.__doc__
    assert 'get_entry(site="research", section="reports", slug)' in doc
