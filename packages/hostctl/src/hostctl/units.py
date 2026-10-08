"""Render the repo's systemd units into this machine's unit folders, filling @REPO@ with the
repo's path and @KEY@ with KEY from host.env:

  host/quadlet/<name>.container.in             -> ~/.config/containers/systemd/<name>.container
  host/systemd/*.service, host/systemd/*.timer -> ~/.config/systemd/user/

  diff     show how the installed units differ from the rendered ones
  install  write the changed ones and reload systemd.
           A container whose unit changed is (re)started; a host unit is
           restarted only if it's running. Enabling host units is up to each `uv run hostctl *-setup`.
           A change to comments alone restarts nothing. Either waits while it's a guarded
           runner with a run going (run_guard), and a container also while its local
           image or its network isn't there yet (its app's setup makes them), or the
           egress proxy it Wants= isn't installed (`uv run hostctl units egress`).
           A host unit this rendered whose template is gone (removed from host/systemd,
           or moved to host/quadlet as a container) is retired: stopped, disabled and
           deleted. A container that took its name over is
           started then, since the old copy would hide Quadlet's unit; `diff` lists them.
           An app's host units stay running while one of its containers can't start yet.
           Given app names (`uv run hostctl units relay`), it installs and retires only
           those apps' units, so services can move into containers one at a time; the
           rest wait for a later run.

Standard library only, like the rest of hostctl.
"""

import argparse
import difflib
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import hostenv

from hostctl import apps, run_guard

ROOT = Path(__file__).resolve().parents[4]

PLACEHOLDER = re.compile(r"@([A-Z_]+)@")
# The first line of a unit rendered here (and, before uv run hostctl, by `make units`).
RENDERED = re.compile(r"# Rendered by `(uv run hostctl|make) units` from ")


@dataclass
class Unit:
    source: Path  # the template in the repo
    dest: Path  # where it's installed
    text: str  # rendered
    service: str  # the unit to restart when it changes
    always: bool  # restart even if it isn't running (containers: that starts them)


def refuse_worktree(what: str) -> None:
    """Exit unless this is the main checkout: a linked worktree (its .git a file, the main
    checkout's a folder) is never what AnythingLLM mounts and the units run."""
    if (ROOT / ".git").is_file():
        raise SystemExit(
            f"{ROOT} is a git worktree; {what} from the main checkout, which is what runs."
        )


def env_file(file: Path) -> dict[str, str]:
    """An env file's KEY=value lines, quotes dropped as systemd's EnvironmentFile= and
    hostenv.env_values drop them; {} when it can't be read. The scripts' one parser."""
    try:
        lines = file.read_text().splitlines()
    except OSError:
        return {}
    values = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            values[key.strip()] = value.strip().strip("'\"")
    return values


def host_settings(file: Path) -> dict[str, str]:
    """host.env's KEY=value lines, under anything the environment already has."""
    values = env_file(file)
    return {**values, **{k: v for k, v in os.environ.items() if k in values}}


# Where deploy puts the skills, in storage (hostctl.sync).
SKILLS = Path("plugins") / "agent-skills"


def storage() -> Path:
    """AnythingLLM's storage, from host.env or the environment; exits if neither has it."""
    found = host_settings(ROOT / "host.env").get("ANYTHINGLLM_STORAGE")
    return Path(
        found
        or os.environ.get("ANYTHINGLLM_STORAGE")
        or sys.exit(
            "ANYTHINGLLM_STORAGE isn't set: copy host.env.example to host.env and fill it in."
        )
    )


def anythingllm_headers(api: str, fresh: bool = False) -> dict[str, str]:
    """The headers for AnythingLLM's internal API (hostenv.anythingllm_headers, with the
    password in storage's .env); exits when it can't log in."""
    try:
        return hostenv.anythingllm_headers(api, storage() / ".env", fresh=fresh)
    except hostenv.LoginFailed as e:
        sys.exit(str(e))


def render(template: str, values: dict[str, str]) -> str:
    missing = sorted({k for k in PLACEHOLDER.findall(template) if not values.get(k)})
    if missing:
        raise SystemExit(
            f"{', '.join(missing)} not set: copy host.env.example to host.env and fill it in."
        )
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


def rendered(template: Path, values: dict[str, str], root: Path = ROOT) -> str:
    """The unit from `template`, under a header that says where it came from."""
    where = template.relative_to(root / "host")
    return (
        f"# Rendered by `uv run hostctl units` from {where} in the EverythingLLM repo, with this\n"
        "# machine's host.env filled in. Edit the template and run it again, not this copy.\n"
        + render(template.read_text(), values)
    )


def planned(
    values: dict[str, str], containers: Path, user: Path, root: Path = ROOT
) -> list[Unit]:
    units = []
    for t in sorted((root / "host" / "quadlet").glob("*.container.in")):
        name = t.name.removesuffix(".in")
        units.append(
            Unit(
                t,
                containers / name,
                rendered(t, values, root),
                name.removesuffix(".container") + ".service",
                True,
            )
        )
    for t in sorted(
        [
            *(root / "host" / "systemd").glob("*.service"),
            *(root / "host" / "systemd").glob("*.timer"),
        ]
    ):
        # A template (x@.service) never runs itself; its instances pick it up when next started.
        units.append(
            Unit(
                t,
                user / t.name,
                rendered(t, values, root),
                "" if "@." in t.name else t.name,
                False,
            )
        )
    return units


def meaning(text: str) -> list[str]:
    """What systemd acts on: no comments or blank lines, and %h spelled out (the old units
    used %h/dev/everythingllm where the rendered ones have the path)."""
    home = str(Path.home())
    return [
        line.strip().replace("%h", home)
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def installed(unit: Unit) -> str:
    return unit.dest.read_text() if unit.dest.is_file() else ""


def changed(units: list[Unit]) -> list[Unit]:
    """Units whose installed copy differs from the rendered one, or that are a symlink (how
    units were installed before they were rendered)."""
    return [u for u in units if u.dest.is_symlink() or installed(u) != u.text]


def replace_file(dest: Path, text: str, mode: int = 0o644) -> None:
    """Replace `dest` with `text`, mode `mode`, through a temp file of a fresh name in its
    folder (O_EXCL), so a symlink left there is never written through; os.replace swaps a
    symlink at `dest` itself, not what it points to."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def install(todo: list[Unit]) -> list[str]:
    """Write the units; returns the units to restart, in order: containers first."""
    restart = []
    for unit in todo:
        old = installed(unit)
        replace_file(unit.dest, unit.text)
        cosmetic = bool(old) and meaning(old) == meaning(unit.text)
        print(f"installed {unit.dest}" + (" (comments only)" if cosmetic else ""))
        if (
            not cosmetic
            and unit.service
            and unit.service not in restart
            and (unit.always or active(unit.service))
        ):
            restart.append(unit.service)
    return sorted(
        restart, key=lambda s: not any(u.always and u.service == s for u in todo)
    )


# Networks podman has without anyone creating them, or that aren't networks.
BUILTIN_NETWORKS = {"podman", "host", "none", "private", "slirp4netns", "pasta"}


def podman_has(kind: str, name: str) -> bool:
    """Whether podman has the image or network `name` (kind "image" or "network")."""
    return (
        subprocess.run(
            ["podman", kind, "exists", name], capture_output=True, check=False
        ).returncode
        == 0
    )


def missing(unit: Unit, plan: list[Unit] = ()) -> list[str]:
    """What a container's unit needs that isn't there yet: an image of ours (localhost/,
    built by its app's `before` step), a network it doesn't create itself, or a container
    of `plan` that it Wants= (the egress proxy) and that isn't installed. Starting it
    without them only fails, or, without the proxy, crash-loops."""
    needs = []
    for line in unit.text.splitlines():
        key, _, value = line.strip().partition("=")
        if key == "Image" and value.startswith("localhost/"):
            needs.append(("image", value))
        elif key == "Network":
            name = value.split(":")[0]
            if name not in BUILTIN_NETWORKS and not name.endswith(".network"):
                needs.append(("network", name))
        elif key == "Wants":
            needs += [("unit", name) for name in value.split()]
    lacking = []
    for kind, name in needs:
        if kind == "unit":
            dep = [u for u in plan if u.always and u.service == name]
            if dep and not dep[0].dest.exists():
                lacking.append(name)
        elif not podman_has(kind, name):
            lacking.append(f"{kind} {name}")
    return lacking


def how_to_make(service: str, lacking: list[str]) -> str:
    """The commands that make what `service`'s container lacks, in the order to run them."""
    app = apps.app_of(service)
    steps = [
        f"`uv run hostctl units {dep.name}`"
        for need in lacking
        if need.endswith(".service") and (dep := apps.app_of(need))
    ]
    if any(not need.endswith(".service") for need in lacking):
        steps.append(f"`uv run hostctl {app.name if app else service}-setup`")
    return ", then ".join(steps)


def hold_back(
    restart: list[str], plan: list[Unit], cleared: set[str] = frozenset()
) -> list[str]:
    """The units of `restart` to restart now. Left out, each with a word on why: a
    container podman can't start yet, and a guarded runner, host unit or container, with
    a run going that the restart would kill (run_guard asks, or FORCE=1), unless retiring
    its host unit already asked (`cleared`)."""
    now = []
    for service in restart:
        app = apps.app_of(service)
        setup = f"`uv run hostctl {app.name if app else service}-setup`"
        containers = [u for u in plan if u.always and u.service == service]
        lacking = [need for u in containers for need in missing(u, plan)]
        if lacking:
            print(
                f"units: not starting {service}: no {', '.join(lacking)} yet; "
                f"{how_to_make(service, lacking)} makes them"
            )
        elif (
            service in run_guard.GUARDED
            and service not in cleared
            and not run_guard.ok_to_restart(service)
        ):
            print(f"units: left {service} running; {setup} applies its new unit later")
        else:
            now.append(service)
    return now


def retired(plan: list[Unit], user: Path, root: Path = ROOT) -> list[Path]:
    """Host units in `user` that this rendered, or linked into the repo's host/systemd the
    old way, and that no template makes any more."""
    planned_here = {u.dest for u in plan}
    linked_from = root / "host" / "systemd"
    old = []
    for path in sorted([*user.glob("*.service"), *user.glob("*.timer")]):
        if path in planned_here:
            continue
        if path.is_symlink():
            ours = Path(os.readlink(path)).parent == linked_from
        else:
            try:
                ours = bool(RENDERED.match(path.read_text()))
            except OSError:
                ours = False
        if ours:
            old.append(path)
    return old


def retire(old: list[Path], plan: list[Unit]) -> tuple[list[str], list[str]]:
    """Stop, disable and delete each. Returns the
    containers to start now that the host unit of their name is gone, and the units left
    as they are: a guarded runner with a run going (run_guard asks), whose container
    mustn't start beside it, and every old unit of an app whose containers can't start
    yet (no image, network or proxy), so the host keeps running it until they can."""
    containers = {u.service for u in plan if u.always}
    waiting = {}  # app name -> what one of its containers lacks
    for u in plan:
        if (
            u.always
            and (lacking := missing(u, plan))
            and (app := apps.app_of(u.service))
        ):
            waiting.setdefault(app.name, (u.service, lacking))
    start, left = [], []
    for path in old:
        name = path.name
        blocked = [a for a in waiting if belongs(name, [a])]
        if blocked:
            service, lacking = waiting[blocked[0]]
            print(
                f"units: left {name} running: {service} has no {', '.join(lacking)} yet; "
                f"{how_to_make(service, lacking)}, then `uv run hostctl units {blocked[0]}`"
            )
            left.append(name)
            continue
        if name in run_guard.GUARDED and not run_guard.ok_to_restart(name):
            print(f"units: left {name} running; run `uv run hostctl units` again later")
            left.append(name)
            continue
        # A template (x@.service) can't be stopped by its name; a running instance
        # finishes, and nothing starts another.
        if "@." not in name:
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", name], check=False
            )
        path.unlink(missing_ok=True)
        if name in containers:
            start.append(name)
            print(f"retired {path}: its container takes over")
        else:
            print(f"retired {path}: the repo has no template for it any more")
    return start, left


def belongs(unit: str, names: list[str]) -> bool:
    """Whether `unit` is one of the apps `names`': the registry says so, or, for a unit
    the registry no longer has (a retired timer), its name starts with the app's."""
    app = apps.app_of(unit)
    if app is not None:
        return app.name in names
    return any(unit == f"{n}.service" or unit.startswith(f"{n}-") for n in names)


def active(service: str) -> bool:
    cmd = ["systemctl", "--user", "is-active", "--quiet", service]
    return subprocess.run(cmd, check=False).returncode == 0


# The states `systemctl is-enabled` answers yes to.
ENABLED = {
    "enabled",
    "enabled-runtime",
    "static",
    "alias",
    "indirect",
    "generated",
    "transient",
}


def enabled(units: list[str]) -> set[str]:
    """Those of `units` that start with the user's session: enabled (a host unit, by its
    app's setup) or generated (a Quadlet container, once `uv run hostctl units` installed
    it), in one call. Exits when systemctl can't say (no user bus): deploy takes out the
    skills of every app that isn't, so a failed call mustn't read as "disabled"."""
    if not units:
        return set()
    cmd = ["systemctl", "--user", "is-enabled", *units]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    states = r.stdout.split()  # one per unit, "not-found" included, when it knows
    if len(states) != len(units):
        why = r.stderr.strip() or r.stdout.strip()
        raise SystemExit(f"systemctl --user is-enabled: {why}")
    return {u for u, state in zip(units, states) if state in ENABLED}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=["diff", "install"])
    parser.add_argument("apps", nargs="*", help="only these apps' units (default all)")
    args = parser.parse_args(argv)
    if unknown := set(args.apps) - set(apps.load()):
        sys.exit(f"units: no app {', '.join(sorted(unknown))}; `uv run hostctl apps`")
    containers = Path(
        os.environ.get("UNITS_CONTAINER_DIR", "~/.config/containers/systemd")
    ).expanduser()
    user = Path(os.environ.get("UNITS_USER_DIR", "~/.config/systemd/user")).expanduser()
    values = {"REPO": str(ROOT), **host_settings(ROOT / "host.env")}
    plan = planned(values, containers, user)
    todo = changed(plan)
    old = retired(plan, user)
    if args.apps:
        todo = [u for u in todo if belongs(u.service, args.apps)]
        old = [p for p in old if belongs(p.name, args.apps)]

    if args.action == "diff":
        for unit in todo:
            sys.stdout.writelines(
                difflib.unified_diff(
                    installed(unit).splitlines(True),
                    unit.text.splitlines(True),
                    f"installed/{unit.dest.name}",
                    f"repo/{unit.source.relative_to(ROOT)}",
                )
            )
        for path in old:
            print(f"retire {path}: no template makes it any more")
        if not (todo or old):
            print("units: installed units match the repo")
        return

    if not (todo or old):
        print("units: nothing to install")
        return
    refuse_worktree("install")
    restart = install(todo)
    start, left = retire(old, plan)
    restart = [s for s in restart if s not in left]
    restart += [s for s in start if s not in restart]
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    # retire() has asked the guard for the containers it starts; don't ask again.
    for service in hold_back(restart, plan, cleared=set(start)):
        subprocess.run(["systemctl", "--user", "restart", service], check=True)
        print(f"restarted {service}")


if __name__ == "__main__":
    main()
