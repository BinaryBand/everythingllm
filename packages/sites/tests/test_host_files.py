"""The repo's host files: the pages site's Caddyfile, the unit templates and the install scripts."""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


units = load("units")
machine = load("machine")


def parse_csp(csp: str) -> dict[str, str]:
    return dict(rule.strip().split(" ", 1) for rule in csp.split(";"))


def caddy_policies() -> dict[str, dict[str, dict[str, str]]]:
    """Each site's CSPs by matcher ("default" for the header with none), by its port."""
    text = (ROOT / "host" / "caddy" / "pages.Caddyfile").read_text()
    sites = re.split(r"^:(\d+) \{$", text, flags=re.MULTILINE)[1:]
    return {
        port: {
            name or "default": parse_csp(csp)
            for name, csp in re.findall(
                r'header (?:@(\w+) )?Content-Security-Policy "([^"]*)"', block
            )
        }
        for port, block in zip(sites[::2], sites[1::2])
    }


def test_pages_csp_still_forbids_scripts():
    """Agent-written pages are only safe while this holds: no scripts, and nothing fetched
    from another host, so CSS can't send anything out either."""
    sites = caddy_policies()
    assert set(sites) == {"8445", "8447"}
    pages, workspaces = sites["8445"], sites["8447"]
    assert set(pages) == {"default"} and set(workspaces) == {"default", "scripts"}
    for rules in (pages["default"], workspaces["default"]):
        assert rules["default-src"] == "'self'" and rules["script-src"] == "'none'"
        assert (
            rules["frame-ancestors"]
            == rules["form-action"]
            == rules["base-uri"]
            == "'none'"
        )
    # The workspaces' pages may use inline CSS; nothing else is loosened.
    assert workspaces["default"] == {
        **pages["default"],
        "style-src": "'self' 'unsafe-inline'",
    }


def test_no_workspace_runs_scripts_yet():
    """The scripts policy is there to be switched on for one workspace, on its own origin
    (:8447); until then its matcher matches nothing, and it only ever adds scripts."""
    workspaces = caddy_policies()["8447"]
    assert workspaces["scripts"] == {
        **workspaces["default"],
        "script-src": "'self' 'unsafe-inline'",
    }
    text = (ROOT / "host" / "caddy" / "pages.Caddyfile").read_text()
    assert re.findall(r"^\s*@scripts (.*)$", text, flags=re.MULTILINE) == [
        "expression false"
    ]


def test_podcasts_send_the_pages_sites_policy():
    """splice-web serves /podcasts on the pages site's origin."""
    from splice.web import CSP

    assert parse_csp(CSP) == caddy_policies()["8445"]["default"]


def plan(tmp_path, values=None):
    values = values or {
        "REPO": "/repo",
        **units.host_settings(ROOT / "host.env.example"),
    }
    return units.planned(values, tmp_path / "containers", tmp_path / "user")


def test_templates_use_only_known_settings_and_not_this_machines_paths(tmp_path):
    planned = {u.dest.name: u for u in plan(tmp_path)}
    assert {
        "anythingllm.container",
        "static_agent.container",
        "log-filter.conf",
        "podcasts-web.service",
        "podcasts-sync.timer",
    } <= set(planned)
    for unit in planned.values():
        assert not units.PLACEHOLDER.search(unit.text), unit.source
        assert "dev/everythingllm" not in unit.source.read_text(), unit.source
    assert "Volume=/repo:/mcp:ro" in planned["anythingllm.container"].text
    assert "EnvironmentFile=/repo/host.env" in planned["podcasts-web.service"].text
    assert (
        planned["log-filter.conf"].dest
        == tmp_path / "containers" / "anythingllm.container.d" / "log-filter.conf"
    )
    assert (planned["log-filter.conf"].service, planned["log-filter.conf"].always) == (
        "anythingllm.service",
        True,
    )
    assert (
        planned["podcasts-web.service"].service,
        planned["podcasts-web.service"].always,
    ) == ("podcasts-web.service", False)


def test_rendering_refuses_a_missing_setting():
    with pytest.raises(SystemExit, match="ANYTHINGLLM_STORAGE not set"):
        units.render("Volume=@ANYTHINGLLM_STORAGE@:/x", {"REPO": "/repo"})


def unit(tmp_path, dest, text, service="x.service", always=False):
    return units.Unit(tmp_path / "src", dest, text, service, always)


def test_install_replaces_links_without_touching_the_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(units, "active", lambda s: True)
    repo = tmp_path / "repo"
    (repo / "x.container.d").mkdir(parents=True)
    (repo / "q.service").write_text("[Service]\nExecStart=%h/run\n")
    (repo / "x.container.d" / "a.conf").write_text("[Container]\nEnvironment=A=1\n")
    user, containers = tmp_path / "user", tmp_path / "containers"
    user.mkdir(), containers.mkdir()
    (user / "q.service").symlink_to(repo / "q.service")
    (containers / "x.container.d").symlink_to(repo / "x.container.d")
    todo = units.changed(
        [
            unit(
                tmp_path,
                user / "q.service",
                f"# rendered\n[Service]\nExecStart={Path.home()}/run\n",
                "q.service",
            ),
            unit(
                tmp_path,
                containers / "x.container.d" / "a.conf",
                "[Container]\nEnvironment=A=2\n",
                "x.service",
                True,
            ),
        ]
    )
    assert len(todo) == 2
    restart = units.install(todo, tmp_path / "backup")
    assert (
        not (user / "q.service").is_symlink()
        and not (containers / "x.container.d").is_symlink()
    )
    assert (repo / "q.service").read_text() == "[Service]\nExecStart=%h/run\n"
    assert (
        repo / "x.container.d" / "a.conf"
    ).read_text() == "[Container]\nEnvironment=A=1\n"
    # q.service only gained a comment and spelled out %h: nothing to restart.
    assert restart == ["x.service"]
    assert (
        tmp_path / "backup" / "x.container.d" / "a.conf"
    ).read_text() == "[Container]\nEnvironment=A=1\n"
    assert units.changed(todo) == []


def test_a_unit_in_a_linked_folder_counts_as_changed_even_if_its_text_matches(tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "a.conf").write_text("same")
    (tmp_path / "x.container.d").symlink_to(tmp_path / "repo")
    u = unit(tmp_path, tmp_path / "x.container.d" / "a.conf", "same")
    assert units.changed([u]) == [u]


def test_a_changed_host_unit_restarts_only_if_running_and_containers_go_first(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(units, "active", lambda s: s == "on.service")
    for name in ("on.service", "off.service", "c.container"):
        (tmp_path / name).write_text("[Service]\nA=1\n")
    restart = units.install(
        [
            unit(tmp_path, tmp_path / "on.service", "[Service]\nA=2\n", "on.service"),
            unit(tmp_path, tmp_path / "off.service", "[Service]\nA=2\n", "off.service"),
            unit(
                tmp_path,
                tmp_path / "c.container",
                "[Service]\nA=2\n",
                "c.service",
                True,
            ),
        ],
        tmp_path / "backup",
    )
    assert restart == ["c.service", "on.service"]


def test_machine_check_wants_host_env(tmp_path, monkeypatch):
    monkeypatch.setattr(machine, "ROOT", tmp_path)
    assert machine.check() == [
        "no host.env: copy host.env.example to host.env and fill it in."
    ]


def test_env_keys_says_which_keys_are_set_but_keeps_no_values(tmp_path):
    (tmp_path / ".env").write_text("A=secret\nB=''\n# C=x\nD=\n")
    assert machine.env_keys(tmp_path) == {"A": True, "B": False, "D": False}
    assert machine.env_keys(tmp_path / "missing") == {}


run_guard = load("run_guard")


def test_restart_asks_while_deep_research_runs(tmp_path, capsys, monkeypatch):
    import json
    import os
    import time

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("FORCE", raising=False)
    running = tmp_path / "research" / "runs" / "running"
    assert run_guard.ok_to_restart(
        "research-runner.service", tmp_path
    )  # no folder: nothing runs
    running.mkdir(parents=True)
    quiet = running / "quiet.json"
    quiet.write_text(
        json.dumps(
            {
                "started": "2026-10-05T07:00:00Z",
                "question": "killed long ago",
                "stale_ms": 180000,
            }
        )
    )
    os.utime(quiet, (time.time() - 600, time.time() - 600))
    assert run_guard.ok_to_restart("research-runner.service", tmp_path)  # not live
    (running / "live.json").write_text(
        json.dumps(
            {
                "started": "2026-10-05T07:34:48Z",
                "question": "Bitcoin teaching?",
                "stale_ms": 180000,
            }
        )
    )
    assert not run_guard.ok_to_restart("research-runner.service", tmp_path)
    err = capsys.readouterr().err
    assert "Bitcoin teaching?" in err and "research-runner" in err
    monkeypatch.setenv("FORCE", "1")
    assert run_guard.ok_to_restart("research-runner.service", tmp_path)
    monkeypatch.delenv("FORCE")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda q: "y")
    assert run_guard.ok_to_restart("research-runner.service", tmp_path)
    monkeypatch.setattr("builtins.input", lambda q: "")
    assert not run_guard.ok_to_restart("research-runner.service", tmp_path)


def test_restart_asks_while_a_delegation_runs(tmp_path, monkeypatch, capsys):
    import json

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.delenv("FORCE", raising=False)
    running = tmp_path / "agents" / "runs" / "running"
    running.mkdir(parents=True)
    (running / "live.json").write_text(
        json.dumps(
            {
                "started": "2026-10-06T07:00:00Z",
                "subject": "compare",
                "stale_ms": 180000,
            }
        )
    )
    assert run_guard.ok_to_restart(
        "research-runner.service", tmp_path
    )  # not research's
    assert not run_guard.ok_to_restart("agents-runner.service", tmp_path)
    err = capsys.readouterr().err
    assert (
        "Delegations going (1)" in err and "compare" in err and "agents-runner" in err
    )
