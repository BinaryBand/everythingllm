"""hostctl's units, machine and run_guard: rendering and installing the unit templates, the
checks before `uv run hostctl install`, and the restart guard."""

import sys
from pathlib import Path

import pytest
from hostctl import machine, run_guard, units

ROOT = Path(__file__).resolve().parents[3]


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
