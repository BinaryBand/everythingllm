"""Sync AnythingLLM config between this repo and the live storage directory.

The repo is the source of truth for skill code and manifests. AnythingLLM
itself writes a few fields into a skill's plugin.json (the enabled toggle and
the setup values entered in the UI); those are kept from the live copy on
deploy so the UI and the repo don't fight.

A skill an app lists (its `skills` in apps.toml) is deployed only while that app is set up
here, its runner enabled (`uv run hostctl <app>-setup` enables it); otherwise deploy takes
its live copy out of storage, so the agent isn't offered a tool whose runner isn't there,
and keeps what the UI set in its plugin.json (KEPT) for when it's deployed again.
A skill the repo dropped stays in storage, as do skills made in the UI.

A workspace's system prompt is AnythingLLM's, and deploy never writes one. It sets
the system prompt's block (system-prompt.md, as hostenv.prompt wraps it) as the
default for new workspaces, and the static System Prompt Variable
everythingllm_version to the repo's version, so a workspace whose block is behind
says so; the update-prompt skill refreshes it. Scheduled jobs have no workspace, so
AnythingLLM gives them its built-in prompt instead.

Slash command presets are AnythingLLM's own: make and change them in the UI.

This machine's settings come from host.env (see host.env.example): ANYTHINGLLM_STORAGE is
where storage is.
"""

import argparse
import difflib
import json
import os
import shutil
import sys
import urllib.error
from pathlib import Path

from hostenv import prompt

from hostctl import apps, run_guard, units
from hostctl.units import ROOT, replace_file, storage

STORAGE = storage()
REPO = ROOT / "anythingllm"
LIVE_SKILLS = STORAGE / units.SKILLS
# What the UI set in each skill deploy took out (<skill>.json, host-only), for its return.
KEPT = run_guard.DATA / "hostctl" / "skills"


def merge_plugin_json(repo_text: str, live_text: str | None) -> str:
    """Repo manifest, with the live `active` flag and setup_args values."""
    repo = json.loads(repo_text)
    if live_text is None:
        return json.dumps(repo, indent=2) + "\n"
    live = json.loads(live_text)
    if "active" in live:
        repo["active"] = live["active"]
    live_args = live.get("setup_args") or {}
    for name, definition in (repo.get("setup_args") or {}).items():
        # A value set in the repo wins; otherwise keep what the UI saved.
        if "value" not in definition and "value" in live_args.get(name, {}):
            definition["value"] = live_args[name]["value"]
    return json.dumps(repo, indent=2) + "\n"


def unset_skills() -> dict[str, str]:
    """The skills whose app isn't set up here (its runner isn't enabled) -> that app."""
    of = apps.skill_apps()
    on = units.enabled(sorted({a.runner for a in of.values() if a.runner}))
    return {skill: app.name for skill, app in of.items() if app.runner not in on}


def planned_files(unset: dict[str, str]) -> dict[Path, str]:
    """Map of live path -> content that a deploy would write, leaving out the `unset`
    skills."""
    out: dict[Path, str] = {}
    for skill in sorted(
        p
        for p in (REPO / "agent-skills").iterdir()
        if p.is_dir() and p.name not in unset
    ):
        for src in sorted(p for p in skill.rglob("*") if p.is_file()):
            dest = LIVE_SKILLS / skill.name / src.relative_to(skill)
            text = src.read_text()
            if src.name == "plugin.json" and src.parent == skill:
                kept = KEPT / f"{skill.name}.json"
                live = dest if dest.exists() else kept  # a skill deploy took out
                text = merge_plugin_json(
                    text, live.read_text() if live.exists() else None
                )
            out[dest] = text
    return out


def planned_removals(unset: dict[str, str]) -> dict[Path, str]:
    """The live copies of the `unset` skills, which a deploy takes out of storage -> the
    app that isn't set up."""
    return {
        LIVE_SKILLS / skill: app
        for skill, app in sorted(unset.items())
        if os.path.lexists(LIVE_SKILLS / skill)
    }


def ui_settings(folder: Path) -> dict:
    """What the UI set in a live skill's plugin.json (what merge_plugin_json keeps), {} if
    there's nothing to read there: never through a symlink the container left."""
    if folder.is_symlink():
        return {}
    try:
        fd = os.open(folder / "plugin.json", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            live = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(live, dict):
        return {}
    args = live.get("setup_args")
    values = {
        name: {"value": d["value"]}
        for name, d in (args.items() if isinstance(args, dict) else ())
        if isinstance(d, dict) and "value" in d
    }
    return {
        **({"active": live["active"]} if "active" in live else {}),
        **({"setup_args": values} if values else {}),
    }


def remove(path: Path) -> None:
    """Remove a live skill's folder, keeping what the UI set (KEPT); one the container left
    as a symlink goes as the link, never what it points to."""
    if settings := ui_settings(path):
        replace_file(KEPT / f"{path.name}.json", json.dumps(settings) + "\n", 0o600)
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def api(method: str, path: str, body: dict | None = None) -> dict:
    """units.api, exiting with what went wrong."""
    try:
        return units.api(method, path, body)
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path}: {e.code} {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"AnythingLLM API not reachable at {units.API}: {e.reason}")


def planned_default() -> str | None:
    """The live default prompt for new workspaces if deploy would change it, else None."""
    live = api("GET", "/system/default-system-prompt")["defaultSystemPrompt"] or ""
    return live if live.strip() != repo_block() else None


def repo_block() -> str:
    return prompt.block(prompt.REPO_PROMPT.read_text())


def planned_variable() -> tuple[dict | None, str] | None:
    """(the live everythingllm_version variable or None, the repo's version) if deploy
    would set it, else None."""
    value = prompt.version(prompt.REPO_PROMPT.read_text())
    live = next(
        (
            v
            for v in api("GET", "/system/prompt-variables")["variables"]
            if v.get("key") == prompt.VARIABLE and v.get("id") is not None
        ),
        None,
    )
    return None if live and live["value"] == value else (live, value)


def write_variable(live: dict | None, value: str) -> None:
    body = {
        "key": prompt.VARIABLE,
        "value": value,
        "description": "The version of EverythingLLM's system prompt (hostctl.prompt)",
    }
    if live is None:
        api("POST", "/system/prompt-variables", body)
    else:
        api("PUT", f"/system/prompt-variables/{live['id']}", body)


def print_text_diff(label: str, old: str, new: str) -> None:
    sys.stdout.writelines(
        difflib.unified_diff(
            (old + "\n").splitlines(keepends=True),
            (new + "\n").splitlines(keepends=True),
            f"live/{label}",
            f"repo/{label}",
        )
    )


def diff() -> bool:
    changed = False
    unset = unset_skills()
    if (live := planned_default()) is not None:
        changed = True
        print_text_diff("system-prompt/default", live.strip(), repo_block())
    if (variable := planned_variable()) is not None:
        changed = True
        live_var, value = variable
        old = live_var["value"] if live_var else "(none)"
        print(f"{{{prompt.VARIABLE}}}: {old} -> {value}")
    for dest, text in planned_files(unset).items():
        old = dest.read_text() if dest.exists() else ""
        if old != text:
            changed = True
            sys.stdout.writelines(
                difflib.unified_diff(
                    old.splitlines(keepends=True),
                    text.splitlines(keepends=True),
                    f"live/{dest.relative_to(STORAGE)}",
                    f"repo/{dest.relative_to(STORAGE)}",
                )
            )
    for path, app in planned_removals(unset).items():
        changed = True
        print(f"remove live/{path.relative_to(STORAGE)}: {app} isn't set up here")
    if not changed:
        print("Live config matches the repo.")
    return changed


def deploy() -> None:
    unset = unset_skills()
    files = {
        d: t
        for d, t in planned_files(unset).items()
        if not d.exists() or d.read_text() != t
    }
    removals = planned_removals(unset)
    default = planned_default()
    variable = planned_variable()
    if not files and not removals and default is None and variable is None:
        print("Nothing to deploy.")
        return
    if default is not None:
        api(
            "POST",
            "/system/default-system-prompt",
            {"defaultSystemPrompt": repo_block()},
        )
        print("deployed the default prompt for new workspaces")
    if variable is not None:
        write_variable(*variable)
        print(f"set {{{prompt.VARIABLE}}} to {variable[1]}")
    for dest, text in files.items():
        replace_file(dest, text)  # never through a symlink the container left
        print(f"deployed {dest}")
        if dest.name == "plugin.json" and dest.parent.parent == LIVE_SKILLS:
            (KEPT / f"{dest.parent.name}.json").unlink(missing_ok=True)  # merged in
    for path, app in removals.items():
        remove(path)
        print(f"removed {path} ({app} isn't set up here: uv run hostctl {app}-setup)")


def symlinks(folder: Path) -> list[str]:
    """The symlinks under `folder`, relative to it: storage is the container's to write,
    and copying one would copy whatever it points to on the host."""
    found = []
    for top, dirs, files in os.walk(folder):
        for name in dirs + files:
            if (Path(top) / name).is_symlink():
                found.append(str((Path(top) / name).relative_to(folder)))
    return sorted(found)


def import_skill(name: str) -> None:
    src = LIVE_SKILLS / name
    dest = REPO / "agent-skills" / name
    if not src.is_dir():
        sys.exit(f"no live skill at {src}")
    if dest.exists():
        sys.exit(
            f"{dest.relative_to(ROOT)} already exists; remove it first to re-import"
        )
    links = symlinks(src)
    if links:
        sys.exit(
            f"{src} holds symlinks ({', '.join(links[:5])}); a skill's files are plain "
            "files, so it wasn't imported"
        )
    shutil.copytree(src, dest, symlinks=True)
    print(f"imported {src} -> {dest.relative_to(ROOT)}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("diff", help="show what deploy would change")
    sub.add_parser("deploy", help="write repo config into live storage")
    imp = sub.add_parser("import-skill", help="copy a live skill into the repo")
    imp.add_argument("name")
    args = parser.parse_args(argv)

    if args.cmd == "diff":
        diff()
    elif args.cmd == "deploy":
        deploy()
    else:
        import_skill(args.name)


if __name__ == "__main__":
    main()
