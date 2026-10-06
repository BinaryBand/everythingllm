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
        "podcasts-sync-worker.container",
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


QUADLET = Path("/usr/libexec/podman/quadlet")  # podman-user-generator links to it


@pytest.mark.skipif(not QUADLET.exists(), reason="no podman Quadlet generator here")
def test_quadlet_takes_every_container_template(tmp_path):
    """Quadlet refuses keys it doesn't know (Memory= and Umask= in podman 5.4, for one), and
    a unit it can't convert just isn't there: render them all as `units` would, and
    convert them without installing anything."""
    import subprocess

    containers = tmp_path / "containers"
    for u in plan(tmp_path):
        if u.dest.is_relative_to(containers):
            u.dest.parent.mkdir(parents=True, exist_ok=True)
            u.dest.write_text(u.text)
    done = subprocess.run(
        [str(QUADLET), "-dryrun", "-user"],
        env={"QUADLET_UNIT_DIRS": str(containers), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    for template in (ROOT / "host" / "quadlet").glob("*.container.in"):
        name = template.name.removesuffix(".container.in")
        assert f"---{name}.service---" in done.stdout, done.stderr


def test_hold_back_waits_for_a_containers_image_and_network(
    tmp_path, monkeypatch, capsys
):
    have = {("network", "egress-net")}
    monkeypatch.setattr(units, "podman_has", lambda kind, name: (kind, name) in have)
    proxy = unit(
        tmp_path,
        tmp_path / "egress-proxy.container",
        "[Container]\nImage=localhost/everythingllm-service\n"
        "Network=egress-net:ip=10.89.79.2\nNetwork=podman\n",
        "egress-proxy.service",
        True,
    )
    other = unit(
        tmp_path,
        tmp_path / "static_agent.container",
        "[Container]\nImage=docker.io/library/caddy:2-alpine\n",
        "static_agent.service",
        True,
    )
    assert units.missing(proxy) == ["image localhost/everythingllm-service"]
    assert units.missing(other) == []
    todo = [proxy, other]
    assert units.hold_back(["egress-proxy.service", "static_agent.service"], todo) == [
        "static_agent.service"
    ]
    assert (
        "not starting egress-proxy.service: no image localhost/everythingllm-service yet; "
        "`uv run hostctl egress-setup` makes them" in capsys.readouterr().out
    )
    have.add(("image", "localhost/everythingllm-service"))
    assert units.hold_back(["egress-proxy.service"], todo) == ["egress-proxy.service"]


def test_hold_back_leaves_a_guarded_runner_with_a_run_going(
    tmp_path, monkeypatch, capsys
):
    # A runner in a container is held back as a host unit is: research's, once it's one.
    monkeypatch.setattr(units, "podman_has", lambda kind, name: True)
    monkeypatch.setattr(run_guard, "ok_to_restart", lambda service: False)
    runner = unit(
        tmp_path,
        tmp_path / "research-runner.container",
        "[Container]\nImage=localhost/everythingllm-service\n",
        "research-runner.service",
        True,
    )
    assert units.hold_back(["research-runner.service", "x.service"], [runner]) == [
        "x.service"
    ]
    assert "left research-runner.service running" in capsys.readouterr().out


RENDERED = (
    "# Rendered by `uv run hostctl units` from systemd/x in the EverythingLLM repo\n"
)


def test_a_rendered_unit_with_no_template_is_retired(tmp_path, monkeypatch, capsys):
    """The podcasts' timers went when their workers came, and a runner's host unit goes
    when its container comes: the installed copies would keep firing, or hide Quadlet's
    unit of the same name. Only units this rendered (or linked the old way) count."""
    ran = []
    monkeypatch.setattr(
        units.subprocess, "run", lambda cmd, **kw: ran.append(" ".join(cmd))
    )
    monkeypatch.setattr(run_guard, "ok_to_restart", lambda service: True)
    user, containers = tmp_path / "user", tmp_path / "containers"
    user.mkdir()
    for name in ("old.timer", "old@.service", "gone-runner.service", "kept.service"):
        (user / name).write_text(RENDERED + "[Unit]\n")
    (user / "made.timer").write_text("# Rendered by `make units` from systemd/made\n")
    (user / "theirs.service").write_text("[Unit]\nDescription=not ours\n")
    (user / "linked.service").symlink_to(ROOT / "host" / "systemd" / "linked.service")
    plan = [
        unit(tmp_path, user / "kept.service", "x", "kept.service"),
        unit(
            tmp_path,
            containers / "gone-runner.container",
            "x",
            "gone-runner.service",
            True,
        ),
    ]
    old = units.retired(plan, user)
    assert [p.name for p in old] == [
        "gone-runner.service",
        "linked.service",
        "made.timer",
        "old.timer",
        "old@.service",
    ]
    start, left = units.retire(old, plan, tmp_path / "backup")
    assert (start, left) == (["gone-runner.service"], [])
    # A template's instances can't be stopped by its name; they finish on their own.
    assert ran == [
        f"systemctl --user disable --now {name}"
        for name in ("gone-runner.service", "linked.service", "made.timer", "old.timer")
    ]
    assert sorted(p.name for p in user.iterdir()) == ["kept.service", "theirs.service"]
    assert (tmp_path / "backup" / "user" / "old.timer").read_text() == (
        RENDERED + "[Unit]\n"
    )
    assert "gone-runner.service: its container takes over" in capsys.readouterr().out


def test_a_guarded_runner_with_a_run_going_isnt_retired(tmp_path, monkeypatch):
    monkeypatch.setattr(run_guard, "ok_to_restart", lambda service: False)
    monkeypatch.setattr(units.subprocess, "run", lambda *a, **kw: pytest.fail("ran"))
    user = tmp_path / "user"
    user.mkdir()
    (user / "research-runner.service").write_text(RENDERED)
    plan = [
        unit(tmp_path, tmp_path / "r.container", "x", "research-runner.service", True)
    ]
    old = units.retired(plan, user)
    assert units.retire(old, plan, tmp_path / "backup") == (
        [],
        ["research-runner.service"],
    )
    assert (user / "research-runner.service").is_file()


def test_the_podcasts_host_units_are_retired_and_their_containers_started(
    tmp_path, monkeypatch
):
    """From timers to workers to containers: on a machine with any of the podcasts' host
    units installed, each goes, the containers take over the runner's and the workers'
    names, and podcasts-web stays."""
    monkeypatch.setattr(units.subprocess, "run", lambda *a, **kw: None)
    planned = plan(tmp_path)
    user = tmp_path / "user"
    user.mkdir()
    old = [
        "podcasts-runner.service",
        "podcasts-sync-worker.service",
        "podcasts-sync.timer",
        "podcasts-sync@.service",
        "podcasts-transcribe-worker.service",
        "podcasts-transcribe.service",
        "podcasts-transcribe.timer",
    ]
    for name in [*old, "podcasts-web.service"]:
        (user / name).write_text(RENDERED)
    retired = units.retired(planned, user)
    assert [p.name for p in retired] == old
    start, left = units.retire(retired, planned, tmp_path / "backup")
    assert start == [
        "podcasts-runner.service",
        "podcasts-sync-worker.service",
        "podcasts-transcribe-worker.service",
    ]
    assert left == [] and [p.name for p in user.iterdir()] == ["podcasts-web.service"]
