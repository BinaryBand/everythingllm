"""Render the repo's systemd units into this machine's unit folders, filling @REPO@ with the
repo's path and @KEY@ with KEY from host.env:

  host/quadlet/<name>.container.in             -> ~/.config/containers/systemd/<name>.container
  host/systemd/<name>.container.d/*.conf       -> ~/.config/containers/systemd/<name>.container.d/
  host/systemd/*.service, host/systemd/*.timer -> ~/.config/systemd/user/

  diff     show how the installed units differ from the rendered ones
  install  write the changed ones (previous versions go to
           ~/.local/share/everythingllm/backups/) and reload systemd.
           A container whose unit or drop-in changed is (re)started; a host unit is
           restarted only if it's running. Enabling host units is up to each `uv run hostctl *-setup`.
           A change to comments alone restarts nothing. Either waits while it's a guarded
           runner with a run going (run_guard), and a container also while its local
           image or its network isn't there yet (its app's setup makes them).
           A host unit this installed that a container template now replaces
           (~/.config/systemd/user/<x>.service beside host/quadlet/<x>.container.in) is
           disabled, saved with the rest and removed, since systemd would prefer it to
           Quadlet's <x>.service; the restart then starts the container in its place.

Standard library only, like the rest of hostctl.
"""

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import apps  # the registry's reader, standard library only

from hostctl import run_guard

ROOT = Path(__file__).resolve().parents[4]

BACKUPS = run_guard.DATA / "backups"  # outside the repo, which the container mounts
PLACEHOLDER = re.compile(r"@([A-Z_]+)@")
RENDERED = "# Rendered by `uv run hostctl units`"  # how every unit we install starts


@dataclass
class Unit:
    source: Path  # the template in the repo
    dest: Path  # where it's installed
    text: str  # rendered
    service: str  # the unit to restart when it changes
    always: bool  # restart even if it isn't running (containers: that starts them)


def env_file(file: Path) -> dict[str, str]:
    """An env file's KEY=value lines, quotes dropped as systemd's EnvironmentFile= and
    hostrpc.env_values drop them; {} when it can't be read. The scripts' one parser."""
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


_tokens: dict[str, str] = {}  # AnythingLLM's API -> this run's login token


def anythingllm_headers(api: str, fresh: bool = False) -> dict[str, str]:
    """The headers for AnythingLLM's internal API: none while it has no password, else a
    Bearer token from logging in with the password in storage's .env, once per run
    (`fresh` logs in again, after a 401). A copy of hostrpc.anythingllm_headers."""
    env = env_file(storage() / ".env")
    if not (env.get("AUTH_TOKEN") and env.get("JWT_SECRET")):
        return {}
    if fresh or api not in _tokens:
        req = urllib.request.Request(
            f"{api.rstrip('/')}/request-token",
            json.dumps({"password": env["AUTH_TOKEN"]}).encode(),
            {"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                token = json.load(res).get("token")
        except urllib.error.HTTPError as e:
            sys.exit(
                f"AnythingLLM refused the password in {storage() / '.env'} ({e.code})"
            )
        except (urllib.error.URLError, OSError, ValueError) as e:
            sys.exit(f"couldn't log in to AnythingLLM at {api}: {e}")
        if not token:
            sys.exit(f"AnythingLLM refused the password in {storage() / '.env'}")
        _tokens[api] = token
    return {"Authorization": f"Bearer {_tokens[api]}"}


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
        f"{RENDERED} from {where} in the EverythingLLM repo, with this\n"
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
    for t in sorted((root / "host" / "systemd").glob("*.container.d/*.conf")):
        units.append(
            Unit(
                t,
                containers / t.parent.name / t.name,
                rendered(t, values, root),
                t.parent.name.removesuffix(".container.d") + ".service",
                True,
            )
        )
    for t in sorted(
        [
            *(root / "host" / "systemd").glob("*.service"),
            *(root / "host" / "systemd").glob("*.timer"),
        ]
    ):
        # A template (podcasts-sync@.service) never runs itself; its instances pick it up when next started.
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


def superseded(user: Path, root: Path = ROOT) -> tuple[list[Path], list[Path]]:
    """Host units in `user` that a container template now replaces: <x>.service for each
    host/quadlet/<x>.container.in. systemd prefers a unit in ~/.config/systemd/user to the
    one Quadlet generates under the same name, so while it's there the container never
    runs. Returns (ours, others): ours were rendered here (or linked into the repo, the old
    way) and are retired; another's is only pointed out."""
    ours, others = [], []
    for t in sorted((root / "host" / "quadlet").glob("*.container.in")):
        old = user / (t.name.removesuffix(".container.in") + ".service")
        if old.is_symlink() or (old.is_file() and old.read_text().startswith(RENDERED)):
            ours.append(old)
        elif old.exists():
            others.append(old)
    return ours, others


def retire(old: list[Path], backup: Path) -> list[str]:
    """Disable and remove host units a container replaces (superseded), saving each to
    `backup`; returns their services, for the caller to restart after a daemon-reload: that
    stops the host unit's process and starts the container under the same name."""
    services = []
    for unit in old:
        subprocess.run(["systemctl", "--user", "disable", unit.name], check=False)
        if not unit.is_symlink():
            saved = backup / unit.parent.name / unit.name
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_text(unit.read_text())
        unit.unlink()
        print(f"retired {unit}: its container replaces it")
        services.append(unit.name)
    return services


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
    """Units whose installed copy differs from the rendered one, or that are a symlink or sit
    in a symlinked folder (how units were installed before they were rendered)."""
    return [
        u
        for u in units
        if u.dest.is_symlink() or u.dest.parent.is_symlink() or installed(u) != u.text
    ]


def install(todo: list[Unit], backup: Path) -> list[str]:
    """Write the units; returns the units to restart, in order: containers first."""
    restart = []
    for unit in todo:
        old = installed(unit)
        folder = unit.dest.parent
        if folder.is_symlink():  # a drop-in folder linked into the repo, the old way
            folder.unlink()
        folder.mkdir(parents=True, exist_ok=True)
        if old:
            saved = backup / unit.dest.parent.name / unit.dest.name
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_text(old)
        tmp = folder / f".{unit.dest.name}.tmp"
        tmp.write_text(unit.text)
        os.replace(tmp, unit.dest)  # replaces a symlink itself, not what it points to
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


def missing(unit: Unit) -> list[str]:
    """What a container's unit needs that podman doesn't have yet: an image of ours
    (localhost/, built by its app's `before` step) or a network it doesn't create itself.
    Starting it without them only fails."""
    needs = []
    for line in unit.text.splitlines():
        key, _, value = line.strip().partition("=")
        if key == "Image" and value.startswith("localhost/"):
            needs.append(("image", value))
        elif key == "Network":
            name = value.split(":")[0]
            if name not in BUILTIN_NETWORKS and not name.endswith(".network"):
                needs.append(("network", name))
    return [f"{kind} {name}" for kind, name in needs if not podman_has(kind, name)]


def hold_back(restart: list[str], todo: list[Unit]) -> list[str]:
    """The units of `restart` to restart now. Left out, each with a word on why: a
    container podman can't start yet, and a guarded runner, host unit or container, with
    a run going that the restart would kill (run_guard asks, or FORCE=1)."""
    now = []
    for service in restart:
        app = apps.app_of(service)
        setup = f"`uv run hostctl {app.name if app else service}-setup`"
        containers = [u for u in todo if u.always and u.service == service]
        lacking = [need for u in containers for need in missing(u)]
        if lacking:
            print(
                f"units: not starting {service}: no {', '.join(lacking)} yet; {setup} makes them"
            )
        elif service in run_guard.GUARDED and not run_guard.ok_to_restart(service):
            print(f"units: left {service} running; {setup} applies its new unit later")
        else:
            now.append(service)
    return now


def active(service: str) -> bool:
    return (
        subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", service], check=False
        ).returncode
        == 0
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=["diff", "install"])
    args = parser.parse_args(argv)
    containers = Path(
        os.environ.get("UNITS_CONTAINER_DIR", "~/.config/containers/systemd")
    ).expanduser()
    user = Path(os.environ.get("UNITS_USER_DIR", "~/.config/systemd/user")).expanduser()
    values = {"REPO": str(ROOT), **host_settings(ROOT / "host.env")}
    todo = changed(planned(values, containers, user))
    old, others = superseded(user)
    for unit in others:
        print(
            f"units: {unit} isn't one of ours, but hides its container's unit; remove it"
        )

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
        for unit in old:
            print(f"units: would retire {unit}: a container replaces it")
        if not (todo or old):
            print("units: installed units match the repo")
        return

    if not (todo or old):
        print("units: nothing to install")
        return
    if (
        ROOT / ".git"
    ).is_file():  # a linked worktree: its .git is a file, the main checkout's a folder
        raise SystemExit(
            f"{ROOT} is a git worktree; install from the main checkout, which the units run from."
        )
    backup = BACKUPS / time.strftime("%Y%m%d-%H%M%S") / "units"
    restart = install(todo, backup)
    # A host unit that became a container: the restart below swaps one for the other.
    restart = [s for s in retire(old, backup) if s not in restart] + restart
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    for service in hold_back(restart, todo):
        subprocess.run(["systemctl", "--user", "restart", service], check=True)
        print(f"restarted {service}")
    if backup.exists():
        print(f"previous versions saved to {backup}")


if __name__ == "__main__":
    main()
