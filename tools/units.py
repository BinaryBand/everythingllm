"""Render the repo's systemd units into this machine's unit folders, filling @REPO@ with the
repo's path and @KEY@ with KEY from host.env:

  host/quadlet/<name>.container.in             -> ~/.config/containers/systemd/<name>.container
  host/systemd/<name>.container.d/*.conf       -> ~/.config/containers/systemd/<name>.container.d/
  host/systemd/*.service, host/systemd/*.timer -> ~/.config/systemd/user/

  diff     show how the installed units differ from the rendered ones
  install  write the changed ones (previous versions go to
           ~/.local/share/everythingllm/backups/) and reload systemd.
           A container whose unit or drop-in changed is (re)started; a host unit is
           restarted only if it's running. Enabling host units is up to each `make *-setup`.
           A change to comments alone restarts nothing.

Standard library only, run with the system `python3`, like sync.py.
"""

import argparse
import difflib
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import research_guard

ROOT = Path(__file__).resolve().parents[1]
BACKUPS = research_guard.DATA / "backups"  # outside the repo, which the container mounts
PLACEHOLDER = re.compile(r"@([A-Z_]+)@")


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
        f"# Rendered by `make units` from {where} in the EverythingLLM repo, with this\n"
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


def active(service: str) -> bool:
    return (
        subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", service], check=False
        ).returncode
        == 0
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=["diff", "install"])
    args = parser.parse_args()
    containers = Path(
        os.environ.get("UNITS_CONTAINER_DIR", "~/.config/containers/systemd")
    ).expanduser()
    user = Path(os.environ.get("UNITS_USER_DIR", "~/.config/systemd/user")).expanduser()
    values = {"REPO": str(ROOT), **host_settings(ROOT / "host.env")}
    todo = changed(planned(values, containers, user))

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
        if not todo:
            print("units: installed units match the repo")
        return

    if not todo:
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
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if research_guard.SERVICE in restart and not research_guard.ok_to_restart():
        restart.remove(research_guard.SERVICE)
        print(
            f"units: left {research_guard.SERVICE} running; `make research-setup` applies its new unit later"
        )
    for service in restart:
        subprocess.run(["systemctl", "--user", "restart", service], check=True)
        print(f"restarted {service}")
    if backup.exists():
        print(f"previous versions saved to {backup.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
