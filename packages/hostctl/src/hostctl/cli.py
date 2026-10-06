"""hostctl's one command: everything that sets up, syncs, checks and follows this machine, by
name (`uv run hostctl deploy`). `uv run hostctl` lists them.

The steps are the other hostctl modules, called in-process; what isn't (systemctl, podman, the
test runs, the skills, the site builds) runs as a subprocess, echoed first.

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
import shlex
import subprocess
import sys
from collections.abc import Callable

import apps  # the registry's reader, standard library only
import tomllib

from hostctl import appctl, gateway_env, machine, units

ROOT = units.ROOT
SERVICE = "anythingllm.service"
CONTAINER = "systemd-anythingllm"
SANDBOX = ROOT / "host" / "containers" / "sandbox"
# image: its Containerfile in SANDBOX
SANDBOX_IMAGES = {
    "localhost/everythingllm-sandbox": "Containerfile.sandbox",
    "localhost/everythingllm-sandbox-proxy": "Containerfile.proxy",
}
# A podman network with no route out and no DNS: (name, subnet).
SANDBOX_NET = ("sandbox-net", "10.89.77.0/24")
# The service containers' image, and their network, whose only way out is the egress proxy:
# its name and subnet are egress.toml's (packages/egress), with the addresses on it.
SERVICE_IMAGE = (
    "localhost/everythingllm-service",
    ROOT / "host" / "containers" / "service",
)
EGRESS_TOML = ROOT / "packages" / "egress" / "src" / "egress" / "egress.toml"
# The MCP servers' venv and uv cache inside the AnythingLLM container.
MCP = "/app/server/storage/everythingllm/mcp"
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
    code = subprocess.run(cmd, cwd=ROOT).returncode
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
    deploy()
    machine.main(["wait-api"])
    machine.main(["search"])
    serve_setup()
    appctl.main(["setup", "--installed"])
    machine.main(["wait-api"])
    try:
        health()
    except SystemExit as e:  # what failed is printed; the checklist still helps
        print(f"health: exit {e.code} (ignored)", flush=True)
    machine.main(["checklist"])


@command(
    "units",
    "render host/quadlet/ and host/systemd/ into this machine's unit folders (backs up first), reload systemd, restart what changed; with app names, only theirs",
)
def install_units(*names: str) -> None:
    units.main(["install", *names])


@command(
    "diff",
    "show what deploy would change in live storage, and where the installed units differ from the repo's",
)
def diff() -> None:
    from hostctl import sync  # needs ANYTHINGLLM_STORAGE

    skills_check()
    sync.main(["diff"])
    units.main(["diff"])


@command(
    "deploy",
    "write skills, jobs, slash commands, the system prompt and MCP config live (backs up first), refresh MCP deps, restart AnythingLLM, rebuild the sites",
)
def deploy() -> None:
    from hostctl import sync  # needs ANYTHINGLLM_STORAGE

    skills_check()
    sync.main(["deploy"])
    mcp_sync()
    restart()
    sites_build()


@command(
    "skills",
    "write the agent skills that forward an op to a host service, from the fronts' `skills` (hostrpc.skillgen)",
)
def skills() -> None:
    run("uv", "run", "--all-packages", "python", "-m", "hostctl.skills")


@command(
    "skills-check",
    "stop if the generated skills don't match their declarations (diff and deploy run it)",
)
def skills_check() -> None:
    run("uv", "run", "--all-packages", "python", "-m", "hostctl.skills", "--check")


@command("import-skill", "copy a live skill into the repo: import-skill <hubId>")
def import_skill(name: str) -> None:
    from hostctl import sync

    sync.main(["import-skill", name])


@command(
    "import-job", 'copy a live scheduled job into the repo: import-job "Daily News Page"'
)
def import_job(name: str) -> None:
    from hostctl import sync

    sync.main(["import-job", name])


@command("import-command", "copy a live slash command into the repo: import-command /foo")
def import_command(name: str) -> None:
    from hostctl import sync

    sync.main(["import-command", name])


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
    "check every app's units, ports and runners, and each MCP server (e.g. after a reboot)",
)
def health() -> None:
    run(str(ROOT / "packages" / "hostctl" / "health.sh"))


@command("test", "run tests for all MCP servers and agent skills")
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


@command(
    "mcp-sync",
    "install/refresh the MCP servers' deps inside the AnythingLLM container, and only theirs",
)
def mcp_sync() -> None:
    """The container's uv syncs one --package at a time: the first sync is exact (it removes
    whatever no MCP server needs), the rest only add."""
    from hostctl import sync

    for i, pkg in enumerate(sync.mcp_packages()):
        run(
            "podman", "exec", "-w", "/tmp",
            "-e", f"UV_PROJECT_ENVIRONMENT={MCP}/venv",
            "-e", f"UV_CACHE_DIR={MCP}/uv-cache",
            "-e", "UV_PYTHON_DOWNLOADS=never",
            CONTAINER, "uv", "sync", "--frozen", "--no-dev", "--package", pkg,
            *(["--inexact"] if i else []), "--project", "/mcp",
        )  # fmt: skip


@command("apps", "list the apps, for <app>-setup and <app>-logs")
def list_apps() -> None:
    appctl.main(["list"])


@command(
    "<app>-setup",
    "set an app up: its steps, tailnet paths, units (restarted; asks first while one of its runs is going, FORCE=1 doesn't) and timers",
)
def setup_app(app: str) -> None:
    install_units()
    appctl.main(["setup", app])


@command("<app>-logs", "follow an app's units (apps lists them)")
def app_logs(app: str) -> None:
    appctl.main(["logs", app])


@command(
    "serve-setup",
    "map the apps' tailnet HTTPS paths with tailscale serve (other mappings are left alone)",
)
def serve_setup() -> None:
    appctl.main(["serve"])


@command(
    "gateway-client",
    "give an MCP gateway client a token (kept if it has one) and print its `claude mcp add` command: gateway-client <name>",
)
def gateway_client(name: str) -> None:
    gateway_env.add_client(name)


@command(
    "sandbox-images",
    "build the sandbox's images and its internal network (sandbox-setup runs this first)",
)
def sandbox_images() -> None:
    for image, containerfile in SANDBOX_IMAGES.items():
        run("podman", "build", "-t", image, "-f", str(SANDBOX / containerfile), str(SANDBOX))
    internal_network(*SANDBOX_NET)


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
    with EGRESS_TOML.open("rb") as f:
        network = tomllib.load(f)["network"]
    internal_network(network["name"], network["subnet"], network["ip_range"])


@command(
    "sites-build",
    "rebuild all Zola sites by hand (sites-runner does this on every write)",
)
def sites_build() -> None:
    os.environ["ANYTHINGLLM_STORAGE"] = str(units.storage())  # stops here without it
    run("uv", "run", "--package", "sites", "sites-build")


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
