"""The hostctl commands that depend on which apps there are, from the apps registry
(packages/apps/src/apps/apps.toml): `uv run hostctl <app>-setup`, `uv run hostctl <app>-logs`, `make
serve-setup`, `uv run hostctl apps`, and the parts of `uv run hostctl install` and `uv run hostctl health` that list apps.

  list               the apps: what each is, and whether `uv run hostctl install` sets it up (or why not)
  setup APP...       for each app: run its `before` steps, map its tailnet paths, enable and
                     (re)start its units (asking first while a guarded one has a run going;
                     FORCE=1 doesn't ask), and enable and start its timers
  setup --installed  the same for every app `uv run hostctl install` sets up
  serve              map every app's tailnet paths that aren't mapped yet (sudo tailscale serve)
  logs APP           follow the app's units and the ones it watches
  health             `name|url` for each HTTP check, for health.sh

Standard library only, like the rest of hostctl.
"""

import argparse
import os
import subprocess
from pathlib import Path

import apps  # the registry's reader, standard library only

from hostctl import run_guard

ROOT = Path(__file__).resolve().parents[4]
# hostctl and the registry's reader, so a `before` step's python3 finds them whichever it is.
PYTHONPATH = f"{ROOT}/packages/hostctl/src:{ROOT}/packages/apps/src"


def app_named(registry: dict[str, apps.App], name: str) -> apps.App:
    if name not in registry:
        raise SystemExit(f"appctl: no app '{name}'; the apps are: {', '.join(registry)}")
    return registry[name]


def systemctl(*args: str) -> None:
    subprocess.run(["systemctl", "--user", *args], check=True)


def serve(mappings: list[apps.Mapping]) -> None:
    """Map what isn't mapped yet; mappings made by hand or by others are left alone."""
    if not mappings:
        return
    status = subprocess.run(
        ["tailscale", "serve", "status"], capture_output=True, text=True, check=True
    ).stdout
    for m in mappings:
        if (m.path in status) if m.path else (f":{m.https} " in status):
            continue
        path = [f"--set-path={m.path}"] if m.path else []
        cmd = ["sudo", "tailscale", "serve", "--bg", f"--https={m.https}", *path, m.target]
        print(" ".join(cmd), flush=True)
        subprocess.run(cmd, check=True)


def setup(app: apps.App) -> None:
    print(f"== {app.name}", flush=True)
    for step in app.before:
        print(step, flush=True)
        env = {**os.environ, "PYTHONPATH": PYTHONPATH}
        if (code := subprocess.run(step, shell=True, cwd=ROOT, env=env).returncode) != 0:
            raise SystemExit(code)
    serve(list(app.serve))
    if app.units:
        units = list(app.units)
        systemctl("enable", *units)
        guard = apps.guarded({app.name: app})
        for unit in units:
            if unit in guard and not run_guard.ok_to_restart(unit):
                raise SystemExit(f"appctl: left {unit} running")
        systemctl("restart", *units)
        print(f"restarted {' '.join(units)}", flush=True)
    if app.timers:
        systemctl("enable", "--now", *app.timers)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    s = sub.add_parser("setup")
    s.add_argument("apps", nargs="*")
    s.add_argument("--installed", action="store_true")
    sub.add_parser("serve")
    sub.add_parser("logs").add_argument("app")
    sub.add_parser("health")
    args = parser.parse_args(argv)
    registry = apps.load()

    if args.cmd == "list":
        for app in registry.values():
            how = (
                "uv run hostctl install"
                if app.install
                else app.why_not_installed and f"not in uv run hostctl install: {app.why_not_installed}"
            )
            print(f"  {app.name:12} {app.summary}" + (f"\n  {'':12} ({how})" if how else ""))
    elif args.cmd == "setup":
        names = [a.name for a in registry.values() if a.install] if args.installed else args.apps
        if not names:
            raise SystemExit("appctl: name the apps to set up, or --installed")
        for app in [app_named(registry, n) for n in names]:
            setup(app)
    elif args.cmd == "serve":
        serve([m for _, m in apps.serve_mappings(registry)])
    elif args.cmd == "logs":
        app = app_named(registry, args.app)
        units = [u for u in app.all_units if not u.endswith(".timer")]
        cmd = ["journalctl", "--user", "-f", *(f"-u{u}" for u in units)]
        os.execvp(cmd[0], cmd)
    elif args.cmd == "health":
        for name, url in apps.health_checks(registry):
            print(f"{name}|{url}")


if __name__ == "__main__":
    main()
