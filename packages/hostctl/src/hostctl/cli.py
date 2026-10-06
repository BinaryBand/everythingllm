"""hostctl's one command: everything that sets up, syncs, checks and follows this machine, by
name (`python3 -m hostctl deploy`; each Makefile target is an alias for the command of the
same name). `python3 -m hostctl` lists them.

The steps are the other hostctl modules, called in-process; what isn't (systemctl, podman, the
test runs, the skills, the site builds) runs as a subprocess, echoed first as make echoed it.

Config (environment):
  PUBLIC_HOST, ANYTHINGLLM_STORAGE  from host.env, or the environment over it; passed on to
                                    every step. Only the steps that touch storage need them.
  FORCE=1                           restart a guarded runner without asking (run_guard)

Standard library only, run with the system `python3`, like the modules it calls.
"""

import inspect
import os
import shlex
import subprocess
import sys
from collections.abc import Callable

import apps  # the registry's reader, standard library only

from hostctl import appctl, machine, units

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
# The MCP servers' venv and uv cache inside the AnythingLLM container.
MCP = "/app/server/storage/everythingllm/mcp"
EXPORTED = ("PUBLIC_HOST", "ANYTHINGLLM_STORAGE")

# name: (help, function); `python3 -m hostctl` lists them in this order.
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
    "render host/quadlet/ and host/systemd/ into this machine's unit folders (backs up first), reload systemd, restart what changed",
)
def install_units() -> None:
    units.main(["install"])


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


@command("import-skill", "copy a live skill into the repo: import-skill foo")
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
    "sandbox-images",
    "build the sandbox's images and its internal network (sandbox-setup runs this first)",
)
def sandbox_images() -> None:
    for image, containerfile in SANDBOX_IMAGES.items():
        run("podman", "build", "-t", image, "-f", str(SANDBOX / containerfile), str(SANDBOX))
    name, subnet = SANDBOX_NET
    if run("podman", "network", "exists", name, check=False):
        run(
            "podman", "network", "create", "--internal", "--disable-dns",
            "--subnet", subnet, name,
        )  # fmt: skip


@command(
    "sites-build", "rebuild all Zola sites by hand (sites-runner does this on every write)"
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
    raise SystemExit(f"hostctl: no command '{name}'; `python3 -m hostctl` lists them")


def usage() -> None:
    print("python3 -m hostctl <command>:")
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
