"""hostctl's one command: everything that sets up, syncs, checks and follows this machine, by
name (`uv run hostctl deploy`). `uv run hostctl` lists them.

The steps are the other hostctl modules, called in-process; what isn't (systemctl, podman, the
test runs) runs as a subprocess, echoed first.

Config (environment):
  PUBLIC_HOST, ANYTHINGLLM_STORAGE  from host.env, or the environment over it; passed on to
                                    every step. Only the steps that touch storage need them.
  FORCE=1                           restart a guarded runner without asking (run_guard)

Standard library only, like the modules it calls.
"""

import inspect
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import tomllib

from hostctl import appctl, apps, gateway_env, machine, run_guard, units

ROOT = units.ROOT
SERVICE = "anythingllm.service"
CONTAINER = "systemd-anythingllm"
# The sandbox's image (sandbox.runner.IMAGE) and its Containerfile's folder. Its runs sit
# on egress-net, at the addresses of egress.toml's sandbox profile.
SANDBOX_IMAGE = "localhost/everythingllm-sandbox"
SANDBOX = ROOT / "host" / "containers" / "sandbox"
# The service containers' image, and their network, whose only way out is the egress proxy:
# its name and subnet are egress.toml's (packages/egress), with the addresses on it.
SERVICE_IMAGE = (
    "localhost/everythingllm-service",
    ROOT / "host" / "containers" / "service",
)
EGRESS_TOML = ROOT / "packages" / "egress" / "src" / "egress" / "egress.toml"
# The workspaces' browsers (packages/browser): their image and its folder, and what
# browser-runner names a workspace's container (browser.runner.IMAGE, PREFIX).
BROWSER = ROOT / "host" / "containers" / "browser"
BROWSER_IMAGE = "localhost/everythingllm-browser"
BROWSER_PREFIX = "everythingllm-browser-"
WORKSPACE_RE = re.compile(r"[a-z0-9_][a-z0-9_-]{0,99}")  # as the sandbox's KEY_RE
EXPORTED = ("PUBLIC_HOST", "ANYTHINGLLM_STORAGE")

# name: (help, function); `uv run hostctl` lists them in this order.
COMMANDS: dict[str, tuple[str, Callable[..., None]]] = {}


def command(name: str, help: str):
    def add(fn: Callable[..., None]) -> Callable[..., None]:
        COMMANDS[name] = (help, fn)
        return fn

    return add


def run(*cmd: str, check: bool = True) -> int:
    """Echo a command and run it from the repo's root; a failure stops hostctl with its code."""
    print(shlex.join(cmd), flush=True)
    code = subprocess.run(cmd, cwd=ROOT, check=False).returncode
    if check and code:
        raise SystemExit(code)
    return code


@command(
    "install",
    "set this machine up from the repo, or bring it up to date; ends with what's left to do in AnythingLLM's UI",
)
def install() -> None:
    machine.main(["check"])
    install_units()
    machine.main(["wait-api"])
    machine.main(["search"])
    try:  # deploy after the setups: it deploys only the skills of apps set up here
        appctl.main(["setup", "--installed"])
    except SystemExit:
        try:  # the apps set up so far get their skills, and the default prompt
            deploy()
        except SystemExit as e:  # the setup's failure is the one to exit with
            print(f"deploy: exit {e.code}", file=sys.stderr, flush=True)
        raise
    deploy()
    machine.main(["wait-api"])
    try:
        health()
    except SystemExit as e:  # what failed is printed; the checklist still helps
        print(f"health: exit {e.code} (ignored)", flush=True)
    machine.main(["checklist"])


@command(
    "units",
    "render host/quadlet/ and host/systemd/ into this machine's unit folders, reload systemd, restart what changed; with app names, only theirs",
)
def install_units(*names: str) -> None:
    units.main(["install", *names])


@command(
    "diff",
    "show what deploy would change in live storage, and where the installed units differ from the repo's",
)
def diff() -> None:
    from hostctl import sync  # needs ANYTHINGLLM_STORAGE

    sync.main(["diff"])
    units.main(["diff"])


@command(
    "deploy",
    "write the skills and the default prompt and its version live, and restart AnythingLLM",
)
def deploy() -> None:
    from hostctl import sync  # needs ANYTHINGLLM_STORAGE

    units.refuse_worktree("deploy")
    sync.main(["deploy"])
    restart()


@command("import-skill", "copy a live skill into the repo: import-skill <hubId>")
def import_skill(name: str) -> None:
    from hostctl import sync

    sync.main(["import-skill", name])


@command(
    "restart",
    "restart AnythingLLM (deep-research runs carry on: they run in research-runner)",
)
def restart() -> None:
    run("systemctl", "--user", "restart", SERVICE)


@command("logs", "follow AnythingLLM's container log")
def logs() -> None:
    os.execvp("podman", ["podman", "logs", "-f", "--tail", "100", CONTAINER])


@command("status", "show AnythingLLM's unit status")
def status() -> None:
    run("systemctl", "--user", "status", SERVICE, "--no-pager")


@command(
    "health",
    "check every app's units, ports and runners (e.g. after a reboot)",
)
def health() -> None:
    run(str(ROOT / "packages" / "hostctl" / "health.sh"))


@command("test", "run every package's tests and the agent skills'")
def test() -> None:
    run("uv", "run", "--all-packages", "--all-extras", "pytest", "-q")
    test_skills()


@command(
    "test-skills",
    "run agent skill and log filter tests inside the AnythingLLM container (its Node, repo at /mcp)",
)
def test_skills() -> None:
    run(
        "podman", "exec", "-e", "NODE_OPTIONS=", "-w", "/tmp", CONTAINER,
        "node", "--test", "/mcp/anythingllm/",
    )  # fmt: skip


@command("apps", "list the apps, for <app>-setup and <app>-logs")
def list_apps() -> None:
    appctl.main(["list"])


@command(
    "<app>-setup",
    "set an app up: its steps, units (restarted; asks first while one of its runs is going, FORCE=1 doesn't) and timers, and print the routes it needs",
)
def setup_app(app: str) -> None:
    # Only its own units: another app's host runner gives way to its container in its
    # own setup (or `units <app>`), one at a time, not as a side effect of this one.
    install_units(app)
    appctl.main(["setup", app])


@command("<app>-logs", "follow an app's units (apps lists them)")
def app_logs(app: str) -> None:
    appctl.main(["logs", app])


@command(
    "routes",
    "list the HTTPS routes this machine must provide to the apps (tailscale serve, Caddy, …), and check each answers on PUBLIC_HOST",
)
def routes() -> None:
    appctl.main(["routes"])


@command(
    "gateway-client",
    "give an MCP gateway client a token (kept if it has one) and print its `claude mcp add` command: gateway-client <name>",
)
def gateway_client(name: str) -> None:
    gateway_env.add_client(name)


@command(
    "sandbox-images",
    "build the sandbox's image and make egress-net, its runs' network (sandbox-setup runs this first)",
)
def sandbox_images() -> None:
    run(
        "podman", "build", "-t", SANDBOX_IMAGE,
        "-f", str(SANDBOX / "Containerfile.sandbox"), str(SANDBOX),
    )  # fmt: skip
    egress_net()


def internal_network(name: str, subnet: str, ip_range: str = "") -> None:
    """Create a podman network with no route out and no DNS, unless it exists as asked.
    `ip_range` is where podman picks an address for a container that names none, so one
    can't take a stopped service's address and its egress profile: a network made without
    it (or with another subnet) is made again, or, while a container uses it, refused."""
    if not run("podman", "network", "exists", name, check=False):
        if network_matches(name, subnet, ip_range):
            return
        print(
            f"{name} isn't on {subnet} with addresses from {ip_range or 'all of it'}; making it again"
        )
        if run("podman", "network", "rm", name, check=False):
            raise SystemExit(
                f"{name} is in use; stop its containers, then run this again"
            )
    run(
        "podman", "network", "create", "--internal", "--disable-dns",
        "--subnet", subnet, *(["--ip-range", ip_range] if ip_range else []),
        name,
    )  # fmt: skip


def network_matches(name: str, subnet: str, ip_range: str) -> bool:
    """Whether podman's network `name` has the one subnet `subnet`, and gives out
    addresses only from `ip_range` (all of the subnet when it's "")."""
    out = subprocess.run(
        ["podman", "network", "inspect", name, "--format", "json"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    subnets = json.loads(out)[0].get("subnets") or []
    if [s.get("subnet") for s in subnets] != [subnet]:
        return False
    lease = subnets[0].get("lease_range")
    if not ip_range:
        return not lease
    pool = ipaddress.ip_network(ip_range)
    return bool(lease) and all(
        ipaddress.ip_address(lease.get(end, "0.0.0.0")) in pool
        for end in ("start_ip", "end_ip")
    )


@command(
    "service-images",
    "build the service containers' image and egress-net, their network (egress-setup runs this first)",
)
def service_images() -> None:
    image, folder = SERVICE_IMAGE
    run(
        "podman", "build", "-t", image, "-f", str(folder / "Containerfile"), str(folder)
    )
    egress_net()


def egress_net() -> None:
    """egress-net, as egress.toml has it."""
    with EGRESS_TOML.open("rb") as f:
        network = tomllib.load(f)["network"]
    internal_network(network["name"], network["subnet"], network["ip_range"])


def browser_data() -> Path:
    """browser-runner's folder: BROWSER_DATA from host.env, its unit's only source for it."""
    found = units.host_settings(ROOT / "host.env").get("BROWSER_DATA")
    return Path(found) if found else run_guard.DATA / "browser"


@command(
    "browser-images",
    "build the workspaces' browser image, copy noVNC out of it for the take-over view, and make egress-net (browser-setup runs this first)",
)
def browser_images() -> None:
    run(
        "podman", "build", "-t", BROWSER_IMAGE, "-f", str(BROWSER / "Containerfile"), str(BROWSER)
    )  # fmt: skip
    egress_net()
    # browser-runner serves noVNC's files to the take-over page from the data dir, the
    # image's copy, so the page and the image's x11vnc come from one build.
    folder = browser_data()
    folder.mkdir(parents=True, exist_ok=True)
    new, old = folder / ".novnc.new", folder / ".novnc.old"
    shutil.rmtree(new, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    name = "everythingllm-browser-novnc-copy"
    run("podman", "rm", "-f", name, check=False)
    run("podman", "create", "--name", name, BROWSER_IMAGE)
    try:
        run("podman", "cp", f"{name}:/opt/novnc", str(new))
    finally:
        run("podman", "rm", "-f", name, check=False)
    if (folder / "novnc").exists():
        (folder / "novnc").rename(old)
    new.rename(folder / "novnc")
    shutil.rmtree(old, ignore_errors=True)


@command(
    "browser-reset",
    "wipe a workspace's browser profile (its sessions, cookies and history; not its saved logins), stopping its browser first: browser-reset <workspace>",
)
def browser_reset(workspace: str) -> None:
    if not WORKSPACE_RE.fullmatch(workspace):
        raise SystemExit(f"browser-reset: '{workspace}' isn't a workspace's slug")
    run("podman", "rm", "-f", "--time", "5", BROWSER_PREFIX + workspace, check=False)
    profile = browser_data() / "profiles" / workspace
    if profile.exists():
        shutil.rmtree(profile)
        print(f"removed {profile}")
    else:
        print(f"{workspace} has no browser profile")


def lookup(name: str) -> tuple[Callable[..., None], list[str]]:
    """The function for a command name and the arguments it gets from the name itself."""
    if name in COMMANDS and not name.startswith("<"):
        return COMMANDS[name][1], []
    app, _, kind = name.rpartition("-")
    if kind in ("setup", "logs") and app in apps.load():
        return COMMANDS[f"<app>-{kind}"][1], [app]
    raise SystemExit(f"hostctl: no command '{name}'; `uv run hostctl` lists them")


def usage() -> None:
    print("uv run hostctl <command>:")
    for name, (help, _) in COMMANDS.items():
        print(f"  {name:18} {help}")


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("help", "-h", "--help"):
        usage()
        return
    fn, args = lookup(argv[0])
    args += argv[1:]
    try:
        inspect.signature(fn).bind(*args)
    except TypeError:
        help = next(help for help, f in COMMANDS.values() if f is fn)
        raise SystemExit(f"hostctl {argv[0]}: {help}") from None
    settings = units.host_settings(ROOT / "host.env")
    os.environ.update({k: settings[k] for k in EXPORTED if k in settings})
    fn(*args)
