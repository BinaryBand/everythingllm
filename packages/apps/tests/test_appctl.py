"""tools/appctl.py: what `make <app>-setup` and `make serve-setup` run, with systemctl,
tailscale and the guard faked."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
spec = importlib.util.spec_from_file_location("appctl", ROOT / "tools" / "appctl.py")
assert spec is not None and spec.loader is not None
appctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(appctl)

STATUS = """https://host:8445 (tailnet only)
|-- /             proxy http://127.0.0.1:8445
|-- /_live/research proxy http://127.0.0.1:8450
"""


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
            appctl.apps.Mapping(8446, 8446),
        ]
    )
    assert ran == [
        "tailscale serve status",
        "sudo tailscale serve --bg --https=8445 --set-path=/_live/agents http://127.0.0.1:8451",
        "sudo tailscale serve --bg --https=8446 http://127.0.0.1:8446",
    ]


def test_setup_runs_its_steps_maps_restarts_and_starts_timers(ran, monkeypatch):
    monkeypatch.setattr(appctl.run_guard, "ok_to_restart", lambda unit: True)
    registry = appctl.apps.load()
    appctl.setup(registry["podcasts"])
    assert ran == [
        "tailscale serve status",
        "sudo tailscale serve --bg --https=8445 --set-path=/podcasts http://127.0.0.1:8449",
        "systemctl --user enable podcasts-runner.service podcasts-web.service",
        "systemctl --user restart podcasts-runner.service podcasts-web.service",
        "systemctl --user enable --now podcasts-sync.timer podcasts-transcribe.timer",
    ]
    ran.clear()
    appctl.setup(registry["agents"])
    assert ran[0] == "python3 tools/agents_env.py"
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


def test_a_failing_step_stops_the_setup(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(appctl.subprocess, "run", run)
    with pytest.raises(SystemExit) as stopped:
        appctl.setup(appctl.apps.load()["relay"])
    assert stopped.value.code == 1 and calls == ["python3 tools/relay_env.py"]
