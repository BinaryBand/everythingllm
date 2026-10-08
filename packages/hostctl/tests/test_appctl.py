"""hostctl.appctl: what `uv run hostctl <app>-setup` and `uv run hostctl routes` run, with
systemctl, the network and the guard faked."""

import contextlib
import ssl
import urllib.error
from types import SimpleNamespace

import pytest
from hostctl import appctl


@pytest.fixture
def ran(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd if isinstance(cmd, str) else " ".join(cmd))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(appctl.subprocess, "run", run)
    return calls


Mapping = appctl.apps.Mapping
PAGES = {"pages": appctl.apps.App("pages", "x", serve=(Mapping(8445, 8445),))}


def answers(monkeypatch, by_url, found=("100.89.16.22",)):
    """Fake urlopen, by_url mapping a URL to a status code or an exception to raise, and
    PUBLIC_HOST resolving to `found`; returns the URLs asked for."""
    asked = []

    def urlopen(url, timeout):
        asked.append(url)
        got = by_url.get(url, 200)
        if isinstance(got, Exception):
            raise got
        if got >= 400:
            raise urllib.error.HTTPError(url, got, "", {}, None)
        return contextlib.nullcontext()

    monkeypatch.setattr(appctl.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(appctl, "addresses", lambda host: list(found))
    return asked


def test_routes_checks_each_on_the_public_host(monkeypatch, capsys):
    asked = answers(
        monkeypatch,
        {
            "https://h.example:8445/": 404,  # a root that's a 404 by design still answers
            "https://h.example:8445/_live/agents/": 502,  # the route's there, its server isn't
            "https://h.example:3001/everythingllm/": urllib.error.URLError(
                ConnectionRefusedError(111, "Connection refused")
            ),
        },
    )
    registry = {
        **PAGES,
        "agents": appctl.apps.App("agents", "x", serve=(Mapping(8445, 8451, "/_live/agents"),)),
        "relay": appctl.apps.App("relay", "x", serve=(Mapping(3001, 8446, "/everythingllm"),)),
    }
    assert appctl.routes(registry, "h.example") is False
    assert sorted(asked) == [
        "https://h.example:3001/everythingllm/",
        "https://h.example:8445/",
        "https://h.example:8445/_live/agents/",
    ]
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "  OK    https://h.example:8445/ -> http://127.0.0.1:8445 (pages)"
    assert out[1] == (
        "  FAIL  https://h.example:8445/_live/agents/ -> http://127.0.0.1:8451 (agents): HTTP 502"
    )
    assert out[2].startswith("  FAIL  https://h.example:3001/everythingllm/") and "refused" in out[2]
    assert appctl.routes(PAGES, "h.example") is True


def test_a_route_without_a_valid_certificate_fails(monkeypatch):
    bad = ssl.SSLCertVerificationError("certificate verify failed")
    bad.verify_message = "hostname mismatch"
    answers(monkeypatch, {"https://h.example:8445/": urllib.error.URLError(bad)})
    assert appctl.route_problem("https://h.example:8445/") == (
        "no valid certificate for the name (hostname mismatch)"
    )


def test_routes_without_a_public_host_fail_and_say_why(capsys):
    assert appctl.routes(PAGES, "") is False
    assert capsys.readouterr().out == (
        "  FAIL  https://<PUBLIC_HOST>:8445/ -> http://127.0.0.1:8445 (pages):"
        " PUBLIC_HOST isn't set in host.env\n"
    )


@pytest.mark.parametrize(
    "found, warned",
    [
        (["100.89.16.22"], False),  # CGNAT, as Tailscale's are
        (["192.168.1.5", "fd7a:115c::1"], False),
        (["100.89.16.22", "203.0.114.7"], True),
    ],
)
def test_a_public_address_is_warned_about_not_failed(monkeypatch, capsys, found, warned):
    answers(monkeypatch, {}, found)
    assert appctl.routes(PAGES, "h.example") is True
    out = capsys.readouterr().out
    assert ("WARN  h.example resolves to a public address (203.0.114.7)" in out) is warned


def test_a_name_on_loopback_fails_since_the_containers_cant_reach_it(monkeypatch, capsys):
    answers(monkeypatch, {}, ["127.0.0.1", "::1"])
    assert appctl.routes(PAGES, "h.example") is False
    assert "FAIL  h.example resolves only to loopback" in capsys.readouterr().out


def test_an_opt_in_app_thats_not_set_up_isnt_checked(monkeypatch, capsys):
    asked = answers(monkeypatch, {"https://h.example:8452/": 502})
    monkeypatch.setattr(appctl, "active", lambda unit: False)
    gateway = appctl.apps.App(
        "gateway",
        "x",
        units={"gateway.service": "MCP gateway"},
        serve=(Mapping(8452, 8452),),
        why_not_installed="opt-in",
    )
    assert appctl.routes({"gateway": gateway}, "h.example") is True
    assert asked == []
    assert capsys.readouterr().out == (
        "  --    https://h.example:8452/ -> http://127.0.0.1:8452 (gateway, not set up)\n"
    )
    # Once one of its units runs, it counts.
    monkeypatch.setattr(appctl, "active", lambda unit: True)
    assert appctl.routes({"gateway": gateway}, "h.example") is False


def test_setup_runs_its_steps_restarts_and_says_its_routes(ran, monkeypatch, capsys):
    monkeypatch.setattr(appctl.run_guard, "ok_to_restart", lambda unit: True)
    monkeypatch.setattr(appctl, "public_host", lambda: "h.example")
    registry = appctl.apps.load()
    appctl.setup(registry["browser"])
    # browser-runner is a host unit: enabled, then restarted.
    assert ran == [
        "python3 -m hostctl browser-images",
        "systemctl --user enable browser-runner.service",
        "systemctl --user restart browser-runner.service",
    ]
    out = capsys.readouterr().out
    assert "  https://h.example:8445/_live/browser/ -> http://127.0.0.1:8453" in out
    assert "  https://h.example:8454/ -> http://127.0.0.1:8454" in out
    assert "its skills reach AnythingLLM with `uv run hostctl deploy` (browse, " in out
    for skill in registry["browser"].skills:
        (appctl.storage() / appctl.SKILLS / skill).mkdir(parents=True)
    appctl.setup(registry["browser"])
    assert "uv run hostctl deploy" not in capsys.readouterr().out
    ran.clear()
    # The relay is a container, which Quadlet enables, so it's only restarted.
    appctl.setup(registry["relay"])
    assert ran == [
        "python3 -m hostctl.relay_env",
        "python3 -m hostctl service-images",
        "systemctl --user restart relay.service",
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
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(appctl.subprocess, "run", run)
    with pytest.raises(SystemExit) as stopped:
        appctl.setup(appctl.apps.load()["relay"])
    assert stopped.value.code == 1 and calls == ["python3 -m hostctl.relay_env"]

