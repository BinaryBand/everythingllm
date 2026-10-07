"""hostctl.appctl: what `uv run hostctl <app>-setup` and `uv run hostctl serve-setup` run, with systemctl,
tailscale and the guard faked."""

import json
from types import SimpleNamespace

import pytest
from hostctl import appctl

# `tailscale serve status --json`: :8445's root and one path, and only a path on :3001.
STATUS = json.dumps(
    {
        "TCP": {"8445": {"HTTPS": True}, "3001": {"HTTPS": True}},
        "Web": {
            "host.ts.net:8445": {
                "Handlers": {
                    "/": {"Proxy": "http://127.0.0.1:8445"},
                    "/_live/research": {"Proxy": "http://127.0.0.1:8450"},
                }
            },
            "host.ts.net:3001": {
                "Handlers": {"/everythingllm": {"Proxy": "http://127.0.0.1:8446"}}
            },
        },
    }
)


@pytest.fixture
def ran(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd if isinstance(cmd, str) else " ".join(cmd))
        return SimpleNamespace(returncode=0, stdout=STATUS)

    monkeypatch.setattr(appctl.subprocess, "run", run)
    return calls


def test_serve_maps_only_whats_missing(ran):
    appctl.serve(
        [
            appctl.apps.Mapping(8445, 8445),
            appctl.apps.Mapping(8445, 8450, "/_live/research"),
            appctl.apps.Mapping(8445, 8451, "/_live/agents"),
            appctl.apps.Mapping(3001, 8446, "/everythingllm"),
            appctl.apps.Mapping(3001, 3001),  # a path on the port isn't its root
            # nor is the same path on another port
            appctl.apps.Mapping(8447, 8447, "/_live/research"),
        ]
    )
    assert ran == [
        "tailscale serve status --json",
        "sudo tailscale serve --bg --https=8445 --set-path=/_live/agents http://127.0.0.1:8451",
        "sudo tailscale serve --bg --https=3001 http://127.0.0.1:3001",
        "sudo tailscale serve --bg --https=8447 --set-path=/_live/research http://127.0.0.1:8447",
    ]


def test_serve_with_nothing_mapped_maps_everything(ran, monkeypatch):
    def run(cmd, **kw):
        ran.append(" ".join(cmd))
        return SimpleNamespace(returncode=0, stdout="{}\n")

    monkeypatch.setattr(appctl.subprocess, "run", run)
    appctl.serve([appctl.apps.Mapping(3001, 8446, "/everythingllm")])
    assert ran[-1] == (
        "sudo tailscale serve --bg --https=3001 --set-path=/everythingllm"
        " http://127.0.0.1:8446"
    )


def test_setup_runs_its_steps_maps_and_restarts(ran, monkeypatch):
    monkeypatch.setattr(appctl.run_guard, "ok_to_restart", lambda unit: True)
    registry = appctl.apps.load()
    appctl.setup(registry["browser"])
    # browser-runner is a host unit: enabled, then restarted.
    assert ran == [
        "python3 -m hostctl browser-images",
        "tailscale serve status --json",
        "sudo tailscale serve --bg --https=8445 --set-path=/_live/browser http://127.0.0.1:8453",
        "sudo tailscale serve --bg --https=8454 http://127.0.0.1:8454",
        "systemctl --user enable browser-runner.service",
        "systemctl --user restart browser-runner.service",
    ]
    ran.clear()
    # sites-runner is a container, which Quadlet enables, so it's only restarted.
    appctl.setup(registry["sites"])
    assert ran == [
        "python3 -m hostctl service-images",
        "tailscale serve status --json",
        "sudo tailscale serve --bg --https=8445 --set-path=/news/write http://127.0.0.1:8448",
        "systemctl --user restart sites-runner.service",
    ]
    ran.clear()
    # No app of ours has a timer now.
    appctl.setup(
        appctl.apps.App("x", "x", units={"x.service": "x"}, timers=("x.timer",))
    )
    assert ran[-1] == "systemctl --user enable --now x.timer"
    ran.clear()
    appctl.setup(registry["agents"])
    assert ran[0] == "python3 -m hostctl.agents_env"
    assert ran[-1] == "systemctl --user restart agents-runner.service"


def test_a_guarded_app_with_a_run_going_isnt_restarted(ran, monkeypatch):
    asked = []
    monkeypatch.setattr(
        appctl.run_guard, "ok_to_restart", lambda unit: asked.append(unit) or False
    )
    with pytest.raises(SystemExit, match="left research-runner.service running"):
        appctl.setup(appctl.apps.load()["research"])
    assert asked == ["research-runner.service"]
    assert not any("restart" in c for c in ran)


def test_a_container_is_restarted_not_enabled(ran, monkeypatch):
    # Quadlet generates its unit and enables it from [Install]; `systemctl enable` refuses.
    monkeypatch.setattr(appctl.run_guard, "ok_to_restart", lambda unit: True)
    appctl.setup(appctl.apps.load()["egress"])
    assert ran == [
        "python3 -m hostctl service-images",
        "systemctl --user restart egress-proxy.service",
    ]
    ran.clear()
    appctl.setup(appctl.apps.load()["searxng"])  # deployed by something else
    assert not any("systemctl" in c for c in ran)


def test_a_guarded_container_with_a_run_going_isnt_restarted(ran, monkeypatch):
    asked = []
    monkeypatch.setattr(
        appctl.run_guard, "ok_to_restart", lambda unit: asked.append(unit) or False
    )
    research = appctl.apps.App(
        "research",
        "x",
        runner="research-runner.service",
        container={"systemd-research-runner": "deep-research runner"},
        guard=appctl.apps.Guard("research/runs", "Deep-research runs"),
    )
    with pytest.raises(SystemExit, match="left research-runner.service running"):
        appctl.setup(research)
    assert asked == ["research-runner.service"]
    assert not any("systemctl" in c for c in ran)


def test_a_failing_step_stops_the_setup(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(appctl.subprocess, "run", run)
    with pytest.raises(SystemExit) as stopped:
        appctl.setup(appctl.apps.load()["relay"])
    assert stopped.value.code == 1 and calls == ["python3 -m hostctl.relay_env"]
