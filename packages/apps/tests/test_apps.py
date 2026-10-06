"""The registry holds what the naming rules used to: every unit template belongs to one app,
every app's units have a template, mappings don't collide, and the ports the registry
declares are the ones the apps' code and units use."""

import re
from pathlib import Path

import apps
import pytest

REPO = Path(__file__).resolve().parents[3]
HOST = REPO / "host"


def template_of(unit: str) -> str:
    """The template a unit comes from: an instance (x@i.service) from x@.service."""
    return re.sub(r"@[^.]+\.", "@.", unit)


def test_it_loads_and_knows_its_fields(tmp_path):
    loaded = apps.load()
    assert list(loaded)[:3] == ["anythingllm", "searxng", "pages"]
    bad = tmp_path / "apps.toml"
    bad.write_text('[x]\nsummary = "x"\nport = 1\n')
    with pytest.raises(ValueError, match=r"\[x\]: unknown field\(s\) \['port'\]"):
        apps.load(bad)


def test_every_unit_template_belongs_to_exactly_one_app():
    loaded = apps.load()
    templates = [
        *(p.name for p in (HOST / "systemd").glob("*.service")),
        *(p.name for p in (HOST / "systemd").glob("*.timer")),
        *(
            p.name.removesuffix(".container.in") + ".service"
            for p in (HOST / "quadlet").glob("*.container.in")
        ),
    ]
    for template in templates:
        owners = [
            a.name
            for a in loaded.values()
            if template in {template_of(u) for u in a.all_units}
        ]
        assert len(owners) == 1, f"{template} belongs to {owners or 'no app'}"


def test_every_app_unit_has_a_template():
    for app in apps.load().values():
        if not app.managed:
            assert not (app.units or app.timers or app.watch), app.name
            continue
        for unit in [*app.units, *app.timers, *app.watch]:
            assert (HOST / "systemd" / template_of(unit)).is_file(), unit
        for container in app.container:
            name = container.removeprefix("systemd-")
            assert (HOST / "quadlet" / f"{name}.container.in").is_file(), container


def test_a_runner_is_one_of_its_apps_units_and_only_runners_are_guarded():
    for app in apps.load().values():
        if app.runner:
            assert app.runner in app.units, app.name
        if app.guard:
            assert app.runner, f"{app.name} is guarded but has no runner"


def test_mappings_and_ports_dont_collide():
    mappings = [m for _, m in apps.serve_mappings()]
    assert len({(m.https, m.path) for m in mappings}) == len(mappings)
    assert len({m.port for m in mappings}) == len(mappings)


def test_setup_steps_exist_and_install_says_why_not():
    from hostctl import cli

    for app in apps.load().values():
        for step in app.before:
            if module := re.fullmatch(r"python3 -m hostctl\.(\w+)", step):
                hostctl = REPO / "packages" / "hostctl" / "src" / "hostctl"
                assert (hostctl / f"{module[1]}.py").is_file(), step
            elif command := re.fullmatch(r"python3 -m hostctl (\S+)", step):
                cli.lookup(command[1])  # exits if there's no such command
            else:
                raise AssertionError(
                    f"{app.name}: a step this test doesn't know: {step}"
                )
        if app.units:  # host units, which a setup step starts
            assert app.install or app.why_not_installed, (
                f"{app.name}: say why `uv run hostctl install` leaves it out"
            )


def test_the_tools_views_are_what_the_audit_had():
    assert apps.runners() == {
        "sandbox-runner": "sandbox",
        "podcasts-runner": "podcasts",
        "research-runner": "research",
        "agents-runner": "agents",
        "sites-runner": "sites",
        "audit-runner": "audit",
    }
    guarded = apps.guarded()
    assert {u: (g.runs, g.noun) for u, g in guarded.items()} == {
        "research-runner.service": ("research/runs", "Deep-research runs"),
        "agents-runner.service": ("agents/runs", "Delegations"),
    }
    assert apps.watched()["systemd-static_agent"] == (
        "CONTAINER_NAME",
        "pages site (Caddy)",
    )
    assert apps.watched()["podcasts-sync@_all.service"] == (
        "_SYSTEMD_USER_UNIT",
        "podcast sync",
    )
    app = apps.app_of("podcasts-transcribe.timer")
    assert app is not None and app.name == "podcasts"
    assert apps.app_of("nothing.service") is None


def port_of(name: str, path: str = "") -> int:
    [m] = [m for app, m in apps.serve_mappings() if app == name and m.path == path] or [
        None
    ]
    assert m is not None, (name, path)
    return m.port


def test_the_ports_are_the_ones_the_code_and_units_use():
    from agents.runner import Settings as AgentsSettings
    from gateway.app import Config as GatewayConfig
    from publicweb.pages import SEARXNG
    from relay.app import Config as RelayConfig
    from research.job import Settings as ResearchSettings
    from sites import articles_web
    from sites.store import PAGES_PORT

    assert port_of("research", "/_live/research") == ResearchSettings.live_port
    assert port_of("agents", "/_live/agents") == AgentsSettings.live_port
    assert port_of("sites", "/news/write") == articles_web.PORT
    assert port_of("relay") == RelayConfig.port
    assert port_of("gateway") == GatewayConfig.port
    pages = {m.port for app, m in apps.serve_mappings() if app == "pages"}
    assert PAGES_PORT in pages
    assert f":{port_of('searxng')}/" in SEARXNG
    web = (HOST / "systemd" / "podcasts-web.service").read_text()
    assert f"--port {port_of('podcasts', '/podcasts')}" in web
    caddy = (HOST / "caddy" / "pages.Caddyfile").read_text()
    assert {int(p) for p in re.findall(r"^:(\d+) \{", caddy, re.MULTILINE)} == pages


def test_the_skills_socket_list_is_the_registrys():
    test = REPO / "anythingllm" / "agent-skills" / "_lib" / "test" / "delegated.test.js"
    named = set(re.findall(r'"([A-Z]+)_SOCKET"', test.read_text()))
    assert named == {name.upper() for name in apps.runners().values()}
