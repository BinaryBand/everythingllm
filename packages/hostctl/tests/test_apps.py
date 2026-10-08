"""The registry holds what the naming rules used to: every unit template belongs to one app,
every app's units have a template, mappings don't collide, and the ports the registry
declares are the ones the apps' code and units use."""

import re
from pathlib import Path

import pytest
from hostctl import apps

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
            assert app.runner in app.all_units and app.runner.endswith(".service"), (
                app.name
            )
            assert not app.runner.endswith(".timer") and app.runner not in app.watch
        if app.guard:
            assert app.runner or len(app.container) == 1, (
                f"{app.name} is guarded but has no runner or one container"
            )


def test_a_runner_may_be_a_containers_service(tmp_path):
    registry = tmp_path / "apps.toml"
    registry.write_text(
        '[research]\nsummary = "x"\nrunner = "research-runner.service"\n'
        'container = { "systemd-research-runner" = "deep-research runner" }\n'
        'guard = { runs = "research/runs", noun = "Deep-research runs" }\n'
    )
    loaded = apps.load(registry)
    research = loaded["research"]
    assert research.container_units == research.all_units == ["research-runner.service"]
    assert apps.runners(loaded) == {"research-runner": "research"}
    assert set(apps.guarded(loaded)) == {"research-runner.service"}
    assert apps.app_of("research-runner.service", loaded) is research


def test_no_container_template_names_its_container():
    # Quadlet names it systemd-<x>, which is how the registry and <app>-logs
    # know it; ContainerName= would change that.
    for template in (HOST / "quadlet").glob("*.container.in"):
        text = template.read_text()
        assert not re.search(r"^ContainerName=", text, re.MULTILINE), template


def test_mappings_and_ports_dont_collide():
    """No two routes on one address, and no port two apps' (one app's server may take
    several paths, as the sandbox's apps server does)."""
    mappings = apps.serve_mappings()
    assert len({(m.https, m.path) for _, m in mappings}) == len(mappings)
    owner: dict[int, str] = {}
    for app, m in mappings:
        assert owner.setdefault(m.port, app) == app, (
            f"{app} and {owner[m.port]} share :{m.port}"
        )


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


def test_the_registrys_views():
    assert apps.runners() == {
        "sandbox-runner": "sandbox",
        "browser-runner": "browser",
        "research-runner": "research",
        "agents-runner": "agents",
    }
    guarded = apps.guarded()
    assert {u: (g.runs, g.noun) for u, g in guarded.items()} == {
        "research-runner.service": ("research/runs", "Deep-research runs"),
        "agents-runner.service": ("agents/runs", "Delegations"),
        "egress-proxy.service": (
            "research/runs",
            "Deep-research runs (their requests go through the proxy)",
        ),
    }
    app = apps.app_of("static_agent.service")
    assert app is not None and app.name == "pages"
    app = apps.app_of("research-runner.service")
    assert app is not None and app.name == "research"
    assert apps.app_of("nothing.service") is None


def port_of(name: str, path: str = "") -> int:
    [m] = [m for app, m in apps.serve_mappings() if app == name and m.path == path] or [
        None
    ]
    assert m is not None, (name, path)
    return m.port


def test_the_ports_are_the_ones_the_code_and_units_use():
    from agents.runner import Settings as AgentsSettings
    from browser.runner import LIVE_PORT as BROWSER_LIVE_PORT
    from browser.runner import TAKEOVER_PORT as BROWSER_TAKEOVER_PORT
    from gateway.app import Config as GatewayConfig
    from publicweb.pages import SEARXNG
    from relay.app import PREFIX as RELAY_PREFIX
    from relay.app import Config as RelayConfig
    from research.job import PAGES_PORT
    from research.job import Settings as ResearchSettings

    from sandbox.appsweb import PORT as APPS_PORT

    assert port_of("sandbox", "/_live/apps") == APPS_PORT
    assert port_of("sandbox", "/_apps") == APPS_PORT
    assert port_of("research", "/_live/research") == ResearchSettings.live_port
    assert port_of("agents", "/_live/agents") == AgentsSettings.live_port
    assert port_of("browser", "/_live/browser") == BROWSER_LIVE_PORT
    assert port_of("browser") == BROWSER_TAKEOVER_PORT
    assert port_of("relay", RELAY_PREFIX) == RelayConfig.port
    assert port_of("gateway") == GatewayConfig.port
    pages = {m.port for app, m in apps.serve_mappings() if app == "pages"}
    assert PAGES_PORT in pages
    assert f":{port_of('searxng')}/" in SEARXNG
    caddy = (HOST / "caddy" / "pages.Caddyfile").read_text()
    assert {int(p) for p in re.findall(r"^:(\d+) \{", caddy, re.MULTILINE)} == pages


def test_a_container_publishes_its_apps_ports_on_the_hosts_loopback():
    """`serve` and the health checks reach a container's port on the host's 127.0.0.1, at
    the port its code listens on: a port a container publishes is one of its app's, the
    same inside and out. An app that runs only containers serves nothing else."""
    for app in apps.load().values():
        published = set()
        for container in app.container:
            name = container.removeprefix("systemd-")
            template = HOST / "quadlet" / f"{name}.container.in"
            if not template.is_file():  # deployed by something else
                continue
            for port in re.findall(
                r"^PublishPort=(\S+)", template.read_text(), re.MULTILINE
            ):
                host, outside, inside = port.split(":")
                assert host == "127.0.0.1" and outside == inside, (name, port)
                published.add(int(outside))
        ports = {m.port for m in app.serve}
        assert published <= ports, app.name
        if app.managed and app.container and not app.units:
            assert ports == published, app.name


def test_the_skills_socket_list_is_the_registrys():
    test = REPO / "anythingllm" / "agent-skills" / "_lib" / "test" / "delegated.test.js"
    named = set(re.findall(r'"([A-Z]+)_SOCKET"', test.read_text()))
    assert named == {name.upper() for name in apps.runners().values()}


def test_a_mapping_is_a_url_on_the_public_host():
    assert apps.Mapping(8445, 8445).url("h.example") == "https://h.example:8445/"
    m = apps.Mapping(3001, 8446, "/everythingllm")
    assert m.describe("h.example") == (
        "https://h.example:3001/everythingllm/ -> http://127.0.0.1:8446"
    )
