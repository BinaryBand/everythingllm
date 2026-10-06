"""Sync AnythingLLM config between this repo and the live storage directory.

The repo is the source of truth for skill code and manifests. AnythingLLM
itself writes a few fields into a skill's plugin.json (the enabled toggle and
the setup values entered in the UI); those are kept from the live copy on
deploy so the UI and the repo don't fight.

Scheduled jobs live in the database, so they go through the AnythingLLM API
(which also reschedules the in-memory cron timer). They are matched by name;
the enabled toggle stays whatever it is live.

The system prompt (system-prompt.md) also goes through the API: it is set on
every workspace and as the default for new ones, except the agents-* workspaces,
whose prompts are their delegation roles' (packages/agents, agents.profiles).
Scheduled jobs have no workspace, so AnythingLLM gives them its built-in prompt
instead.

Slash command presets are in the database too and are matched by command name.
Presets made only in the UI are left alone.

This machine's settings come from host.env (see host.env.example): ANYTHINGLLM_STORAGE is
where storage is.
"""

import argparse
import difflib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from units import BACKUPS, ROOT, anythingllm_headers, storage

STORAGE = storage()
REPO = ROOT / "anythingllm"
LIVE_SKILLS = STORAGE / "plugins" / "agent-skills"
LIVE_MCP = STORAGE / "plugins" / "anythingllm_mcp_servers.json"
REPO_JOBS = REPO / "scheduled-jobs"
API = os.environ.get("ANYTHINGLLM_API", "http://127.0.0.1:3001/api")
JOB_FIELDS = ("prompt", "tools", "schedule")
REPO_PROMPT = REPO / "system-prompt.md"
# Delegation's role workspaces: agents-runner sets their prompts (agents.profiles).
DELEGATED = "agents-"
REPO_COMMANDS = REPO / "slash-commands"
COMMAND_FIELDS = ("prompt", "description")


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


def planned_files() -> dict[Path, str]:
    """Map of live path -> content that a deploy would write."""
    out: dict[Path, str] = {}
    for skill in sorted(p for p in (REPO / "agent-skills").iterdir() if p.is_dir()):
        for src in sorted(p for p in skill.rglob("*") if p.is_file()):
            dest = LIVE_SKILLS / skill.name / src.relative_to(skill)
            text = src.read_text()
            if src.name == "plugin.json" and src.parent == skill:
                text = merge_plugin_json(
                    text, dest.read_text() if dest.exists() else None
                )
            out[dest] = text
    out[LIVE_MCP] = (REPO / "mcp_servers.json").read_text()
    return out


def api(method: str, path: str, body: dict | None = None, fresh: bool = False) -> dict:
    """Call AnythingLLM's internal API, logged in if it has a password (once more after a 401)."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", **anythingllm_headers(API, fresh)}
    req = urllib.request.Request(API + path, data, headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 401 and not fresh:
            return api(method, path, body, fresh=True)
        sys.exit(f"{method} {path}: {e.code} {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"AnythingLLM API not reachable at {API}: {e.reason}")


def repo_jobs() -> dict[str, dict]:
    """Jobs in the repo by name: <slug>/job.json (name, schedule, tools) + prompt.md."""
    jobs = {}
    if REPO_JOBS.is_dir():
        for d in sorted(p for p in REPO_JOBS.iterdir() if p.is_dir()):
            job = json.loads((d / "job.json").read_text())
            job.setdefault("tools", [])
            job["prompt"] = (d / "prompt.md").read_text().strip()
            jobs[job["name"]] = job
    return jobs


def live_jobs() -> dict[str, dict]:
    jobs = {}
    for job in api("GET", "/scheduled-jobs")["jobs"]:
        job["tools"] = json.loads(job["tools"]) if job.get("tools") else []
        jobs[job["name"]] = job
    return jobs


def planned(
    repo: dict[str, dict], fetch_live, fields: tuple[str, ...]
) -> list[tuple[dict, dict | None]]:
    """(repo record, live record or None) for each repo record that a deploy would
    write; records are matched by key and compared on `fields`."""
    if not repo:
        return []
    live = fetch_live()
    return [
        (rec, live.get(key))
        for key, rec in repo.items()
        if key not in live or any(rec[f] != live[key][f] for f in fields)
    ]


def planned_prompts() -> list[tuple[str, str]]:
    """(target, live prompt) for each place the repo system prompt would change:
    "default" (the default for new workspaces) or a workspace slug."""
    if not REPO_PROMPT.exists():
        return []
    prompt = REPO_PROMPT.read_text().strip()
    live = [
        ("default", api("GET", "/system/default-system-prompt")["defaultSystemPrompt"])
    ]
    live += [
        (w["slug"], w["openAiPrompt"] or "")
        for w in api("GET", "/workspaces")["workspaces"]
        if not w["slug"].startswith(DELEGATED)
    ]
    return [(target, text) for target, text in live if text.strip() != prompt]


def write_prompt(target: str, prompt: str) -> None:
    if target == "default":
        api("POST", "/system/default-system-prompt", {"defaultSystemPrompt": prompt})
    else:
        api("POST", f"/workspace/{target}/update", {"openAiPrompt": prompt})


def repo_commands() -> dict[str, dict]:
    """Presets in the repo by command: <name>/command.json (description) + prompt.md,
    where /<name> is the command. The name must already be in the form AnythingLLM
    stores, so it matches the live preset; the server rejects built-ins like /reset."""
    commands = {}
    if REPO_COMMANDS.is_dir():
        for d in sorted(p for p in REPO_COMMANDS.iterdir() if p.is_dir()):
            if not re.fullmatch(r"[a-z0-9_-]{2,}", d.name):
                sys.exit(
                    f"{d.relative_to(ROOT)}: use only a-z, 0-9, _ and - in the name"
                )
            meta = json.loads((d / "command.json").read_text())
            commands["/" + d.name] = {
                "command": "/" + d.name,
                "description": meta.get("description", ""),
                "prompt": (d / "prompt.md").read_text().strip(),
            }
    return commands


def live_commands() -> dict[str, dict]:
    return {
        p["command"]: p for p in api("GET", "/system/slash-command-presets")["presets"]
    }


def print_text_diff(label: str, old: str, new: str) -> None:
    sys.stdout.writelines(
        difflib.unified_diff(
            (old + "\n").splitlines(keepends=True),
            (new + "\n").splitlines(keepends=True),
            f"live/{label}",
            f"repo/{label}",
        )
    )


def print_command_diff(cmd: dict, live: dict | None) -> None:
    label = f"slash-commands{cmd['command']}"
    if live is None:
        print(f"new slash command {cmd['command']} ({cmd['description']})")
        return
    if cmd["description"] != live["description"]:
        print(f"{label} description: {live['description']!r} -> {cmd['description']!r}")
    if cmd["prompt"] != live["prompt"]:
        print_text_diff(f"{label}/prompt", live["prompt"], cmd["prompt"])


def print_job_diff(job: dict, live: dict | None) -> None:
    label = f"scheduled-jobs/{job['name']}"
    if live is None:
        print(f"new job {label} ({job['schedule']}, tools {job['tools']})")
        return
    for field in ("schedule", "tools"):
        if job[field] != live[field]:
            print(f"{label} {field}: {live[field]!r} -> {job[field]!r}")
    if job["prompt"] != live["prompt"]:
        print_text_diff(f"{label}/prompt", live["prompt"], job["prompt"])


def diff() -> bool:
    changed = False
    for target, live in planned_prompts():
        changed = True
        sys.stdout.writelines(
            difflib.unified_diff(
                (live.strip() + "\n").splitlines(keepends=True),
                REPO_PROMPT.read_text().strip().splitlines(keepends=True) + ["\n"],
                f"live/system-prompt/{target}",
                "repo/system-prompt.md",
            )
        )
    for job, live in planned(repo_jobs(), live_jobs, JOB_FIELDS):
        changed = True
        print_job_diff(job, live)
    for cmd, live in planned(repo_commands(), live_commands, COMMAND_FIELDS):
        changed = True
        print_command_diff(cmd, live)
    for dest, text in planned_files().items():
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
    if not changed:
        print("Live config matches the repo.")
    return changed


def save_backup(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def deploy() -> None:
    files = {
        d: t for d, t in planned_files().items() if not d.exists() or d.read_text() != t
    }
    jobs = planned(repo_jobs(), live_jobs, JOB_FIELDS)
    prompts = planned_prompts()
    commands = planned(repo_commands(), live_commands, COMMAND_FIELDS)
    if not files and not jobs and not prompts and not commands:
        print("Nothing to deploy.")
        return
    backup = BACKUPS / time.strftime("%Y%m%d-%H%M%S")
    for target, live in prompts:
        save_backup(backup / "system-prompt" / f"{target}.md", live.strip() + "\n")
        write_prompt(target, REPO_PROMPT.read_text().strip())
        print(
            f"deployed system prompt to {target if target == 'default' else 'workspace ' + target}"
        )
    for job, live in jobs:
        body = {f: job[f] for f in JOB_FIELDS}
        if live is None:
            created = api("POST", "/scheduled-jobs/new", {"name": job["name"], **body})[
                "job"
            ]
            print(
                f"created scheduled job {job['name']!r} (id {created['id']}, enabled)"
            )
            continue
        save_backup(
            backup / "scheduled-jobs" / f"{live['id']}.json",
            json.dumps(live, indent=2) + "\n",
        )
        api("PUT", f"/scheduled-jobs/{live['id']}", body)
        state = "enabled" if live["enabled"] else "disabled"
        print(f"deployed scheduled job {job['name']!r} (id {live['id']}, {state})")
    for cmd, live in commands:
        if live is None:
            created = api("POST", "/system/slash-command-presets", cmd)["preset"]
            print(f"created slash command {cmd['command']} (id {created['id']})")
            continue
        save_backup(
            backup / "slash-commands" / f"{live['id']}.json",
            json.dumps(live, indent=2) + "\n",
        )
        api("POST", f"/system/slash-command-presets/{live['id']}", cmd)
        print(f"deployed slash command {cmd['command']} (id {live['id']})")
    for dest, text in files.items():
        if dest.exists():
            saved = backup / dest.relative_to(STORAGE)
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, saved)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.tmp")
        tmp.write_text(text)
        os.replace(tmp, dest)
        print(f"deployed {dest}")
    if backup.exists():
        print(f"previous versions saved to {backup}")


def import_skill(name: str) -> None:
    src = LIVE_SKILLS / name
    dest = REPO / "agent-skills" / name
    if not src.is_dir():
        sys.exit(f"no live skill at {src}")
    if dest.exists():
        sys.exit(
            f"{dest.relative_to(ROOT)} already exists; remove it first to re-import"
        )
    shutil.copytree(src, dest)
    print(f"imported {src} -> {dest.relative_to(ROOT)}")


def write_repo_dir(dest: Path, meta_file: str, meta: dict, prompt: str) -> None:
    """Write an imported record as <dest>/<meta_file> + prompt.md."""
    if dest.exists():
        sys.exit(
            f"{dest.relative_to(ROOT)} already exists; remove it first to re-import"
        )
    dest.mkdir(parents=True)
    (dest / meta_file).write_text(json.dumps(meta, indent=2) + "\n")
    (dest / "prompt.md").write_text(prompt + "\n")


def import_job(name: str) -> None:
    live = live_jobs().get(name)
    if live is None:
        sys.exit(f"no live scheduled job named {name!r}")
    slug = "-".join("".join(c if c.isalnum() else " " for c in name.lower()).split())
    dest = REPO_JOBS / slug
    meta = {"name": live["name"], "schedule": live["schedule"], "tools": live["tools"]}
    write_repo_dir(dest, "job.json", meta, live["prompt"])
    print(f"imported scheduled job {name!r} -> {dest.relative_to(ROOT)}")


def import_command(name: str) -> None:
    command = "/" + name.removeprefix("/")
    live = live_commands().get(command)
    if live is None:
        sys.exit(f"no live slash command {command}")
    dest = REPO_COMMANDS / command.removeprefix("/")
    write_repo_dir(
        dest, "command.json", {"description": live["description"]}, live["prompt"]
    )
    print(f"imported slash command {command} -> {dest.relative_to(ROOT)}")


def mcp_packages() -> list[str]:
    """The workspace members the MCP servers run (each one's `--package`), so `make mcp-sync`
    installs what they need into the container's venv and nothing else."""
    servers = json.loads((REPO / "mcp_servers.json").read_text())["mcpServers"]
    names = [
        s["args"][s["args"].index("--package") + 1]
        for s in servers.values()
        if "--package" in s["args"]
    ]
    return list(dict.fromkeys(names))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("diff", help="show what deploy would change")
    sub.add_parser("deploy", help="write repo config into live storage")
    imp = sub.add_parser("import-skill", help="copy a live skill into the repo")
    imp.add_argument("name")
    imp_job = sub.add_parser(
        "import-job", help="copy a live scheduled job into the repo"
    )
    imp_job.add_argument("name", help="the job's name as shown in the UI")
    imp_cmd = sub.add_parser(
        "import-command", help="copy a live slash command into the repo"
    )
    imp_cmd.add_argument("name", help="the command, e.g. /foo")
    sub.add_parser(
        "mcp-packages", help="print the workspace members the MCP servers run"
    )
    args = parser.parse_args()

    if args.cmd == "mcp-packages":
        print(" ".join(mcp_packages()))
    elif args.cmd == "diff":
        diff()
    elif args.cmd == "deploy":
        deploy()
    elif args.cmd == "import-job":
        import_job(args.name)
    elif args.cmd == "import-command":
        import_command(args.name)
    else:
        import_skill(args.name)


if __name__ == "__main__":
    main()
