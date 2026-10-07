"""The registry of the apps this repo runs (apps.toml, next to this file), read into records.

Each app's units, socket, tailnet mappings, restart guard, health checks and setup steps are
declared there once; hostctl asks this module rather than keep copies or work
them out from unit names. App code doesn't read it. Standard library only, like hostctl, so
any python3 with packages/apps/src on PYTHONPATH can import it (health.sh, the `before` steps).

    apps = load()                 # name -> App, in the file's order
    runners(), guarded(), app_of(unit), serve_mappings(), health_checks()
"""

from dataclasses import dataclass, field
from pathlib import Path

import tomllib

REGISTRY = Path(__file__).with_name("apps.toml")
FIELDS = {
    "summary",
    "runner",
    "units",
    "timers",
    "watch",
    "container",
    "managed",
    "before",
    "guard",
    "serve",
    "health",
    "install",
    "why_not_installed",
}


@dataclass(frozen=True)
class Mapping:
    """A tailnet HTTPS mapping (tailscale serve): https://<host>:<https><path> -> 127.0.0.1:<port>."""

    https: int
    port: int
    path: str = ""

    @property
    def target(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@dataclass(frozen=True)
class Guard:
    runs: str  # the run log's folder under the data dir (~/.local/share/everythingllm)
    noun: str  # what its runs are called, in the guard's question


@dataclass(frozen=True)
class App:
    name: str
    summary: str
    runner: str | None = None  # serves <name>/runner.sock: a unit, or a container's
    units: dict[str, str] = field(default_factory=dict)  # unit -> label
    timers: tuple[str, ...] = ()
    watch: dict[str, str] = field(default_factory=dict)  # unit -> label
    container: dict[str, str] = field(default_factory=dict)  # systemd-<x> -> label
    managed: bool = True
    before: tuple[str, ...] = ()
    guard: Guard | None = None
    serve: tuple[Mapping, ...] = ()
    health: dict[str, str] = field(default_factory=dict)  # name -> URL
    install: bool = False
    why_not_installed: str = ""

    @property
    def container_units(self) -> list[str]:
        """The units Quadlet generates for its containers: <x>.service for systemd-<x>."""
        return [f"{c.removeprefix('systemd-')}.service" for c in self.container]

    @property
    def all_units(self) -> list[str]:
        """Every unit that belongs to it: its units, timers and watched units, and its
        containers' generated <x>.service."""
        return [*self.units, *self.timers, *self.watch, *self.container_units]


def _app(name: str, raw: dict) -> App:
    unknown = set(raw) - FIELDS
    if unknown:
        raise ValueError(f"apps.toml [{name}]: unknown field(s) {sorted(unknown)}")
    guard = raw.get("guard")
    return App(
        name=name,
        summary=raw.get("summary", ""),
        runner=raw.get("runner"),
        units=dict(raw.get("units", {})),
        timers=tuple(raw.get("timers", ())),
        watch=dict(raw.get("watch", {})),
        container=dict(raw.get("container", {})),
        managed=raw.get("managed", True),
        before=tuple(raw.get("before", ())),
        guard=Guard(**guard) if guard else None,
        serve=tuple(Mapping(**m) for m in raw.get("serve", ())),
        health=dict(raw.get("health", {})),
        install=raw.get("install", False),
        why_not_installed=raw.get("why_not_installed", ""),
    )


def load(path: Path = REGISTRY) -> dict[str, App]:
    """Every app, by name, in the file's order; ValueError for a field it doesn't know."""
    with path.open("rb") as f:
        return {name: _app(name, raw) for name, raw in tomllib.load(f).items()}


def runners(apps: dict[str, App] | None = None) -> dict[str, str]:
    """The host services behind a socket: unit name without .service -> socket folder."""
    return {
        app.runner.removesuffix(".service"): app.name
        for app in (apps or load()).values()
        if app.runner
    }


def guarded(apps: dict[str, App] | None = None) -> dict[str, Guard]:
    """Units whose restarts wait while a run is going: the runner unit, or for an app
    with no runner its one container's unit (the egress proxy, which carries research's
    runs) -> its guard."""
    out = {}
    for app in (apps or load()).values():
        if app.guard and (unit := app.runner or next(iter(app.container_units), None)):
            out[unit] = app.guard
    return out


def app_of(unit: str, apps: dict[str, App] | None = None) -> App | None:
    """The app a unit belongs to, if any."""
    return next(
        (app for app in (apps or load()).values() if unit in app.all_units), None
    )


def serve_mappings(apps: dict[str, App] | None = None) -> list[tuple[str, Mapping]]:
    return [(app.name, m) for app in (apps or load()).values() for m in app.serve]


def health_checks(apps: dict[str, App] | None = None) -> list[tuple[str, str]]:
    return [
        (name, url)
        for app in (apps or load()).values()
        for name, url in app.health.items()
    ]
