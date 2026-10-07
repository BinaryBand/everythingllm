"""The hostctl commands that depend on which apps there are, from the apps registry
(apps.toml, next to this file): `uv run hostctl <app>-setup`, `uv run hostctl <app>-logs`,
`uv run hostctl routes`, `uv run hostctl apps`, and the parts of `uv run hostctl install` and `uv run hostctl health` that list apps.

  list               the apps: what each is, and whether `uv run hostctl install` sets it up (or why not)
  setup APP...       for each app: run its `before` steps, enable and (re)start its units and (re)start its containers (asking first while a
                     guarded one has a run going; FORCE=1 doesn't ask), and enable and start
                     its timers, and print the HTTPS routes it needs from the machine
  setup --installed  the same for every app `uv run hostctl install` sets up
  routes             the HTTPS routes the machine must provide (each app's `serve`), each
                     checked over https://PUBLIC_HOST with OK/FAIL (an app `install`
                     leaves out only once one of its units runs); a FAIL too when
                     PUBLIC_HOST resolves only to loopback, and a WARN when it resolves to
                     a public address; exits 1 if anything failed
  logs APP           follow the app's units and the ones it watches
  health             `name|url` for each HTTP check, for health.sh
  units              the units health.sh checks, one per line: every app's units and watched
                     units, and its containers' <x>.service
  sockets            ping every app's runner on its socket in storage, all at once; prints
                     OK/FAIL lines for health.sh and exits 1 if one didn't answer or has
                     problems (the sandbox runner checks its image, network and proxy)

The routes themselves are the machine's to provide (tailscale serve, Caddy, nginx, …), from
the host, so the servers see 127.0.0.1 as the peer (hostrpc.local_peer); hostctl only says
what they are and checks they answer.

Config (environment):
  PUBLIC_HOST  the HTTPS name this machine is reached by (host.env, or the environment over it)

Standard library only, like the rest of hostctl.
"""

import argparse
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hostctl import apps, run_guard
from hostctl.units import active, host_settings, storage

ROOT = Path(__file__).resolve().parents[4]
# hostctl, so a `before` step's python3 finds it whichever it is.
PYTHONPATH = f"{ROOT}/packages/hostctl/src"
PING_SECONDS = 5  # a runner that's up answers at once
ROUTE_SECONDS = 5


def app_named(registry: dict[str, apps.App], name: str) -> apps.App:
    if name not in registry:
        raise SystemExit(f"appctl: no app '{name}'; the apps are: {', '.join(registry)}")
    return registry[name]


def systemctl(*args: str) -> None:
    subprocess.run(["systemctl", "--user", *args], check=True)


def public_host() -> str:
    return host_settings(ROOT / "host.env").get("PUBLIC_HOST", "")


def route_problem(url: str, timeout: float = ROUTE_SECONDS) -> str:
    """Why `url` doesn't answer as a route should (a verified TLS connection and a status
    below 500, as health.sh holds the loopback checks to), or "" when it does. A missing
    path on a port whose root is routed can't be told from the server's own 404."""
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return ""
    except urllib.error.HTTPError as e:
        return "" if e.code < 500 else f"HTTP {e.code}"
    except OSError as e:  # URLError among them, with its cause in .reason
        reason = getattr(e, "reason", e)
        if isinstance(reason, ssl.SSLCertVerificationError):
            return f"no valid certificate for the name ({reason.verify_message})"
        return f"{type(reason).__name__}: {reason}" if isinstance(reason, OSError) else str(reason)


def addresses(host: str) -> list[str]:
    """The addresses `host` resolves to here, as the egress proxy resolves it (podman's
    network asks the host's resolver)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    return sorted({info[4][0].split("%")[0] for info in infos})


def host_problems(host: str) -> tuple[str, str]:
    """(why PUBLIC_HOST can't work, why it's a worry), each "" when it isn't."""
    found = [ipaddress.ip_address(a) for a in addresses(host)]
    fail = warn = ""
    if found and all(a.is_loopback for a in found):
        fail = (
            f"{host} resolves only to loopback: the service containers reach AnythingLLM and"
            " SearXNG by it through the egress proxy, so it must be an address of this machine"
            " the routes listen on, not 127.0.0.1"
        )
    if public := [str(a) for a in found if a.is_global]:  # not private, loopback or CGNAT
        warn = (
            f"{host} resolves to a public address ({', '.join(public)}): nothing here asks"
            " who's calling on the pages site, and AnythingLLM only by its password, so keep"
            " these ports to your own devices"
        )
    return fail, warn


def set_up(app: apps.App) -> bool:
    """Whether the app's routes are wanted: always, unless it's one `install` leaves out
    (why_not_installed), which counts once one of its units is running."""
    return not app.why_not_installed or any(
        active(u) for u in app.all_units if not u.endswith(".timer")
    )


def route_report(registry: dict[str, apps.App], host: str) -> tuple[list[str], bool]:
    """A line for each app's route, OK/FAIL (-- for an app that isn't set up), and for
    PUBLIC_HOST's own problems; and whether every wanted route answers and the name works."""
    shown = host or "<PUBLIC_HOST>"
    lines, mappings = [], []
    for app in registry.values():
        if app.serve and set_up(app):
            mappings += [(app.name, m) for m in app.serve]
        else:
            lines += [f"  --    {m.describe(shown)} ({app.name}, not set up)" for m in app.serve]
    if host:
        with ThreadPoolExecutor(len(mappings) or 1) as pool:
            why = list(pool.map(lambda nm: route_problem(nm[1].url(host)), mappings))
        fail, warn = host_problems(host)
    else:
        why, fail, warn = ["PUBLIC_HOST isn't set in host.env"] * len(mappings), "", ""
    for (name, m), problem in zip(mappings, why):
        lines.append(
            f"  {'FAIL' if problem else 'OK  '}  {m.describe(shown)} ({name})"
            + (f": {problem}" if problem else "")
        )
    if fail:
        lines.append(f"  FAIL  {fail}")
    if warn:
        lines.append(f"  WARN  {warn}")
    return lines, not any(why) and not fail


def routes(registry: dict[str, apps.App], host: str) -> bool:
    """Print route_report's lines; True when it's all fine."""
    lines, ok = route_report(registry, host)
    print("\n".join(lines))
    return ok


def setup(app: apps.App) -> None:
    print(f"== {app.name}", flush=True)
    for step in app.before:
        print(step, flush=True)
        env = {**os.environ, "PYTHONPATH": PYTHONPATH}
        if (code := subprocess.run(step, shell=True, cwd=ROOT, env=env).returncode) != 0:
            raise SystemExit(code)
    if app.units:
        systemctl("enable", *app.units)
    # A container's unit is generated by Quadlet, whose [Install] enables it: `systemctl
    # enable` refuses one, so it's only restarted. One deployed by something else is
    # left to that.
    units = [*app.units, *(app.container_units if app.managed else [])]
    guard = apps.guarded({app.name: app})
    for unit in units:
        if unit in guard and not run_guard.ok_to_restart(unit):
            raise SystemExit(f"appctl: left {unit} running")
    if units:
        systemctl("restart", *units)
        print(f"restarted {' '.join(units)}", flush=True)
    if app.timers:
        systemctl("enable", "--now", *app.timers)
    if app.serve:
        host = public_host() or "<PUBLIC_HOST>"
        print("routes it needs from the machine (uv run hostctl routes checks them):", flush=True)
        for m in app.serve:
            print(f"  {m.describe(host)}", flush=True)


def ping(sock: Path, timeout: float = PING_SECONDS) -> str:
    """Why the runner on `sock` isn't fine (hostrpc's protocol: a line of JSON each
    way), or "" when it answers `ping` with no problems."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(str(sock))
            with conn.makefile("rwb") as f:
                f.write(json.dumps({"op": "ping", "args": {}}).encode() + b"\n")
                f.flush()
                line = f.readline()
    except (FileNotFoundError, ConnectionRefusedError) as e:
        return f"not running ({type(e).__name__} on {sock})"
    except TimeoutError:
        return f"no answer within {timeout:.0f}s"
    except OSError as e:
        return f"{type(e).__name__}: {e}"
    try:
        reply = json.loads(line)
    except ValueError:
        return "closed the connection without answering" if not line else "a reply that isn't JSON"
    if not reply.get("ok"):
        return reply.get("error") or "unknown error"
    return "; ".join((reply.get("result") or {}).get("problems") or [])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    s = sub.add_parser("setup")
    s.add_argument("apps", nargs="*")
    s.add_argument("--installed", action="store_true")
    sub.add_parser("routes")
    sub.add_parser("logs").add_argument("app")
    sub.add_parser("health")
    sub.add_parser("units")
    sub.add_parser("sockets")
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
    elif args.cmd == "routes":
        raise SystemExit(0 if routes(registry, public_host()) else 1)
    elif args.cmd == "logs":
        app = app_named(registry, args.app)
        units = [u for u in app.all_units if not u.endswith(".timer")]
        cmd = ["journalctl", "--user", "-f", *(f"-u{u}" for u in units)]
        os.execvp(cmd[0], cmd)
    elif args.cmd == "health":
        for name, url in apps.health_checks(registry):
            print(f"{name}|{url}")
    elif args.cmd == "units":
        for app in registry.values():
            for unit in app.all_units:
                if not unit.endswith(".timer"):
                    print(unit)
    elif args.cmd == "sockets":
        runners = apps.runners(registry)
        folder = storage() / "everythingllm"
        with ThreadPoolExecutor(len(runners) or 1) as pool:
            why = list(pool.map(lambda f: ping(folder / f / "runner.sock"), runners.values()))
        for name, problem in zip(runners, why):
            print(f"  {'FAIL' if problem else 'OK  '}  {name}" + (f": {problem}" if problem else ""))
        raise SystemExit(1 if any(why) else 0)


if __name__ == "__main__":
    main()
