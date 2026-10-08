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
        "browser-runner.service",
        "research-runner.container",
    } <= set(planned)
    for unit in planned.values():
        assert not units.PLACEHOLDER.search(unit.text), unit.source
        assert "dev/everythingllm" not in unit.source.read_text(), unit.source
    assert "Volume=/repo:/mcp:ro" in planned["anythingllm.container"].text
    assert (  # the log filter and the patches (anythingllm/), preloaded
        'Environment="NODE_OPTIONS=--require=/mcp/anythingllm/log-filter.js '
        "--require=/mcp/anythingllm/thread-scope.js "
        "--require=/mcp/anythingllm/agent-stop.js "
        '--require=/mcp/anythingllm/job-guard.js"'
        in planned["anythingllm.container"].text
    )
    assert "EnvironmentFile=/repo/host.env" in planned["browser-runner.service"].text
    assert (
        planned["browser-runner.service"].service,
        planned["browser-runner.service"].always,
    ) == ("browser-runner.service", False)


def test_rendering_refuses_a_missing_setting():
    with pytest.raises(SystemExit, match="ANYTHINGLLM_STORAGE not set"):
        units.render("Volume=@ANYTHINGLLM_STORAGE@:/x", {"REPO": "/repo"})


def unit(tmp_path, dest, text, service="x.service", always=False):
    return units.Unit(tmp_path / "src", dest, text, service, always)


def test_install_replaces_links_without_touching_the_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(units, "active", lambda s: True)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "q.service").write_text("[Service]\nExecStart=%h/run\n")
    (repo / "x.container").write_text("[Container]\nEnvironment=A=1\n")
    user, containers = tmp_path / "user", tmp_path / "containers"
    user.mkdir(), containers.mkdir()
    (user / "q.service").symlink_to(repo / "q.service")
    (containers / "x.container").symlink_to(repo / "x.container")
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
                containers / "x.container",
                "[Container]\nEnvironment=A=2\n",
                "x.service",
                True,
            ),
        ]
    )
    assert len(todo) == 2
    restart = units.install(todo)
    assert (
        not (user / "q.service").is_symlink()
        and not (containers / "x.container").is_symlink()
    )
    assert (repo / "q.service").read_text() == "[Service]\nExecStart=%h/run\n"
    assert (repo / "x.container").read_text() == "[Container]\nEnvironment=A=1\n"
    # q.service only gained a comment and spelled out %h: nothing to restart.
    assert restart == ["x.service"]
    assert units.changed(todo) == []


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
    # egress-proxy is guarded: never ask the live data dir whether a run is going.
    monkeypatch.setattr(run_guard, "ok_to_restart", lambda service: True)
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


def test_a_container_waits_for_the_proxy_it_wants(tmp_path, monkeypatch, capsys):
    """`units research` before `units egress` would start containers whose first uv sync
    can't reach PyPI, and they'd crash-loop: a container held to the egress proxy waits
    until the proxy's unit is installed."""
    monkeypatch.setattr(units, "podman_has", lambda kind, name: True)
    proxy = unit(
        tmp_path, tmp_path / "egress-proxy.container", "x", "egress-proxy.service", True
    )
    relay = unit(
        tmp_path,
        tmp_path / "relay.container",
        "[Unit]\nWants=egress-proxy.service\n[Container]\nImage=localhost/everythingllm-service\n",
        "relay.service",
        True,
    )
    assert units.missing(relay, [proxy, relay]) == ["egress-proxy.service"]
    assert units.hold_back(["relay.service"], [proxy, relay]) == []
    assert (
        "not starting relay.service: no egress-proxy.service yet; "
        "`uv run hostctl units egress` makes them" in capsys.readouterr().out
    )
    proxy.dest.write_text("x")
    assert units.missing(relay, [proxy, relay]) == []
    assert units.hold_back(["relay.service"], [proxy, relay]) == ["relay.service"]


def test_a_runner_cleared_when_its_host_unit_retired_isnt_asked_again(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(units, "podman_has", lambda kind, name: True)
    monkeypatch.setattr(
        run_guard, "ok_to_restart", lambda service: pytest.fail("asked again")
    )
    runner = unit(
        tmp_path,
        tmp_path / "research-runner.container",
        "[Container]\nImage=localhost/everythingllm-service\n",
        "research-runner.service",
        True,
    )
    assert units.hold_back(
        ["research-runner.service"], [runner], cleared={"research-runner.service"}
    ) == ["research-runner.service"]


RENDERED = (
    "# Rendered by `uv run hostctl units` from systemd/x in the EverythingLLM repo\n"
)


def test_a_rendered_unit_with_no_template_is_retired(tmp_path, monkeypatch, capsys):
    """A timer goes when its app's workers come, and a runner's host unit goes when its
    container comes: the installed copies would keep firing, or hide Quadlet's
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
    old = units.retired(plan, user, containers)
    assert [p.name for p in old] == [
        "gone-runner.service",
        "linked.service",
        "made.timer",
        "old.timer",
        "old@.service",
    ]
    start, left = units.retire(old, plan)
    assert (start, left) == (["gone-runner.service"], [])
    # A template's instances can't be stopped by its name; they finish on their own.
    assert ran == [
        f"systemctl --user disable --now {name}"
        for name in ("gone-runner.service", "linked.service", "made.timer", "old.timer")
    ]
    assert sorted(p.name for p in user.iterdir()) == ["kept.service", "theirs.service"]
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
    old = units.retired(plan, user, tmp_path / "containers")
    assert units.retire(old, plan) == (
        [],
        ["research-runner.service"],
    )
    assert (user / "research-runner.service").is_file()


def test_an_apps_host_units_stay_while_its_containers_cant_start(
    tmp_path, monkeypatch, capsys
):
    """Retiring research-runner's host unit before its image or network exists would leave
    research with no runner at all: the host unit, and the rest of the app's, stay."""
    monkeypatch.setattr(units.subprocess, "run", lambda *a, **kw: pytest.fail("ran"))
    monkeypatch.setattr(units, "podman_has", lambda kind, name: kind == "network")
    monkeypatch.setattr(
        run_guard, "ok_to_restart", lambda service: pytest.fail("asked")
    )
    planned = plan(tmp_path)
    user = tmp_path / "user"
    user.mkdir()
    for name in ("research-runner.service", "research-old.timer"):
        (user / name).write_text(RENDERED)
    old = units.retired(planned, user, tmp_path / "containers")
    start, left = units.retire(old, planned)
    assert (start, sorted(left)) == (
        [],
        ["research-old.timer", "research-runner.service"],
    )
    assert sorted(p.name for p in user.iterdir()) == [
        "research-old.timer",
        "research-runner.service",
    ]
    out = capsys.readouterr().out
    assert (
        "left research-runner.service running: research-runner.service has no "
        "egress-proxy.service, image localhost/everythingllm-service yet; "
        "`uv run hostctl units egress`, then `uv run hostctl research-setup`, "
        "then `uv run hostctl units research`" in out
    )


def test_an_archived_apps_host_units_are_all_retired(tmp_path, monkeypatch):
    """An app taken out of the repo (podcasts, the audit) leaves its installed host units
    behind: each goes, and no container takes its name."""
    monkeypatch.setattr(units.subprocess, "run", lambda *a, **kw: None)
    monkeypatch.setattr(units, "missing", lambda unit, plan=(): [])
    planned = plan(tmp_path)
    user = tmp_path / "user"
    user.mkdir()
    old = ["audit-runner.service", "podcasts-sync.timer", "podcasts-web.service"]
    for name in [*old, "browser-runner.service"]:
        (user / name).write_text(RENDERED)
    retired = units.retired(planned, user, tmp_path / "containers")
    assert [p.name for p in retired] == old
    assert units.retire(retired, planned) == ([], [])
    assert [p.name for p in user.iterdir()] == ["browser-runner.service"]


def test_units_retires_the_old_host_unit_then_starts_its_container(
    tmp_path, monkeypatch, capsys
):
    import subprocess

    user, containers = tmp_path / "user", tmp_path / "containers"
    user.mkdir()
    (user / "relay.service").write_text(RENDERED + "[Service]\nExecStart=old\n")
    # Everything else is installed as the repo has it, so only the old relay is left.
    planned = plan(tmp_path)
    for u in planned:
        u.dest.parent.mkdir(parents=True, exist_ok=True)
        u.dest.write_text(u.text)
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(units.subprocess, "run", run)
    monkeypatch.setattr(units, "ROOT", tmp_path)  # not a worktree
    monkeypatch.setattr(units, "host_settings", lambda f: {})
    monkeypatch.setattr(units, "planned", lambda values, c, u: planned)
    monkeypatch.setenv("UNITS_USER_DIR", str(user))
    monkeypatch.setenv("UNITS_CONTAINER_DIR", str(containers))

    units.main(["diff"])
    assert f"retire {user / 'relay.service'}" in capsys.readouterr().out
    units.main(["install"])
    systemctl = [c[2:] for c in calls if c[:2] == ["systemctl", "--user"]]
    assert systemctl == [
        ["disable", "--now", "relay.service"],
        ["daemon-reload"],
        ["restart", "relay.service"],
    ]
    assert not (user / "relay.service").exists()


def test_units_retires_an_orphaned_container_before_writing_its_host_unit(
    tmp_path, monkeypatch, capsys
):
    """A container whose template is gone (its service moved back to the host) is stopped
    and deleted before the host unit of its name is written, which would otherwise take
    the name over from the running container at the reload. Quadlet's units refuse
    `disable`. Only containers this rendered count: not someone's own, nor a link."""
    import subprocess

    user, containers = tmp_path / "user", tmp_path / "containers"
    planned = plan(tmp_path)
    for u in planned:
        u.dest.parent.mkdir(parents=True, exist_ok=True)
        u.dest.write_text(u.text)
    host = unit(
        tmp_path, user / "old-runner.service", "[Service]\n", "old-runner.service"
    )
    planned.append(host)
    (containers / "old-runner.container").write_text(RENDERED + "[Container]\n")
    (containers / "searxng.container").write_text("[Container]\nImage=searxng\n")
    (tmp_path / "linked.container").write_text(RENDERED)
    (containers / "linked.container").symlink_to(tmp_path / "linked.container")
    calls = []

    def run(cmd, **kw):
        if cmd[2:3] == ["stop"]:
            assert not host.dest.exists(), "its host unit was written first"
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 3 if "is-active" in cmd else 0)

    monkeypatch.setattr(units.subprocess, "run", run)
    monkeypatch.setattr(units, "ROOT", tmp_path)  # not a worktree
    monkeypatch.setattr(units, "host_settings", lambda f: {})
    monkeypatch.setattr(units, "planned", lambda values, c, u: planned)
    monkeypatch.setenv("UNITS_USER_DIR", str(user))
    monkeypatch.setenv("UNITS_CONTAINER_DIR", str(containers))

    units.main(["diff"])
    out = capsys.readouterr().out
    assert f"retire {containers / 'old-runner.container'}: no template" in out
    assert "searxng" not in out and "linked" not in out
    units.main(["install"])
    systemctl = [c[2:] for c in calls if c[:2] == ["systemctl", "--user"]]
    # The host unit isn't running, so it waits to be enabled.
    assert systemctl == [
        ["stop", "old-runner.service"],
        ["is-active", "--quiet", "old-runner.service"],
        ["daemon-reload"],
    ]
    assert host.dest.exists()
    assert sorted(p.name for p in containers.glob("*.container")) == sorted(
        [
            *(u.dest.name for u in planned if u.always),
            "linked.container",
            "searxng.container",
        ]
    )
    assert (containers / "linked.container").is_symlink()


def test_units_leaves_essential_and_busy_containers_and_their_host_units(
    tmp_path, monkeypatch, capsys
):
    """AnythingLLM's and the proxy's containers take everything down with them, and
    research-runner's has a run going: each stays, with a word on why, and the host unit
    of research-runner's name waits too, since it would take the name over."""
    import subprocess

    user, containers = tmp_path / "user", tmp_path / "containers"
    gone = ("anythingllm", "egress-proxy", "research-runner")
    planned = [u for u in plan(tmp_path) if u.dest.stem not in gone]
    for u in planned:
        u.dest.parent.mkdir(parents=True, exist_ok=True)
        u.dest.write_text(u.text)
    for name in gone:
        (containers / f"{name}.container").write_text(RENDERED + "[Container]\n")
    host = unit(
        tmp_path,
        user / "research-runner.service",
        "[Service]\n",
        "research-runner.service",
    )
    planned.append(host)
    calls, asked = [], []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(units.subprocess, "run", run)
    monkeypatch.setattr(run_guard, "ok_to_restart", lambda s: asked.append(s) or False)
    monkeypatch.setattr(units, "ROOT", tmp_path)  # not a worktree
    monkeypatch.setattr(units, "host_settings", lambda f: {})
    monkeypatch.setattr(units, "planned", lambda values, c, u: planned)
    monkeypatch.setenv("UNITS_USER_DIR", str(user))
    monkeypatch.setenv("UNITS_CONTAINER_DIR", str(containers))

    units.main(["diff"])
    out = capsys.readouterr().out
    assert f"keep {containers / 'anythingllm.container'}: no template" in out
    assert f"retire {containers / 'research-runner.container'}" in out
    units.main(["install"])
    assert asked == ["research-runner.service"]
    systemctl = [c[2:] for c in calls if c[:2] == ["systemctl", "--user"]]
    assert systemctl == [["daemon-reload"]]
    assert sorted(p.name for p in containers.glob("*.container")) == sorted(
        [*(u.dest.name for u in planned if u.always), *(f"{n}.container" for n in gone)]
    )
    assert not host.dest.exists()
    out = capsys.readouterr().out
    for name, why in (
        ("anythingllm", "AnythingLLM and every skill go with it"),
        ("egress-proxy", "every service container's way out goes with it"),
    ):
        assert (
            f"left {containers / name}.container: no template makes it, but {why}"
            in out
        )
    assert "left research-runner.service running" in out
    assert f"not installing {host.dest}" in out


def test_units_for_some_apps_moves_only_their_services(tmp_path, monkeypatch, capsys):
    """`uv run hostctl units relay` switches the relay alone; research-runner's old host unit
    and research's old timer wait for theirs."""
    import subprocess

    user, containers = tmp_path / "user", tmp_path / "containers"
    user.mkdir()
    for name in ("relay.service", "research-runner.service", "research-old.timer"):
        (user / name).write_text(RENDERED + "[Service]\nExecStart=old\n")
    planned = plan(tmp_path)
    for u in planned:  # installed as the repo has them, but the two containers
        if u.service not in ("relay.service", "research-runner.service"):
            u.dest.parent.mkdir(parents=True, exist_ok=True)
            u.dest.write_text(u.text)
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(units.subprocess, "run", run)
    monkeypatch.setattr(units, "ROOT", tmp_path)  # not a worktree
    monkeypatch.setattr(units, "host_settings", lambda f: {})
    monkeypatch.setattr(units, "planned", lambda values, c, u: planned)
    monkeypatch.setenv("UNITS_USER_DIR", str(user))
    monkeypatch.setenv("UNITS_CONTAINER_DIR", str(containers))

    units.main(["install", "relay"])
    systemctl = [c[2:] for c in calls if c[:2] == ["systemctl", "--user"]]
    assert systemctl == [
        ["disable", "--now", "relay.service"],
        ["daemon-reload"],
        ["restart", "relay.service"],
    ]
    assert (containers / "relay.container").exists()
    assert not (containers / "research-runner.container").exists()
    assert not (user / "relay.service").exists()
    assert (user / "research-old.timer").exists()
    assert (user / "research-runner.service").exists()
    calls.clear()
    # Its own app moves its runner, and drops a timer the registry no longer has.
    units.main(["install", "research"])
    assert ["systemctl", "--user", "disable", "--now", "research-old.timer"] in calls
    assert not (user / "research-old.timer").exists()
    assert not (user / "research-runner.service").exists()
    assert (containers / "research-runner.container").exists()
    with pytest.raises(SystemExit, match="no app nope"):
        units.main(["install", "nope"])


def test_enabled_asks_once_and_reads_a_state_per_unit(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return units.subprocess.CompletedProcess(
            cmd, 0, "enabled\nnot-found\ngenerated\n", ""
        )

    monkeypatch.setattr(units.subprocess, "run", run)
    assert units.enabled(["a.service", "b.service", "c.service"]) == {
        "a.service",
        "c.service",
    }
    assert len(calls) == 1 and calls[0][-3:] == ["a.service", "b.service", "c.service"]
    assert units.enabled([]) == set() and len(calls) == 1
