"""Sync AnythingLLM config between this repo and the live storage directory.

The repo is the source of truth for skill code and manifests. AnythingLLM
itself writes a few fields into a skill's plugin.json (the enabled toggle and
the setup values entered in the UI); those are kept from the live copy on
deploy so the UI and the repo don't fight.

Scheduled jobs live in the database, so they go through the AnythingLLM API
(which also reschedules the in-memory cron timer). They are matched by name
(hostctl.jobs), so two live jobs with a repo job's name stop deploy rather than have it
write the wrong one; the enabled toggle stays whatever it is live.

A workspace's system prompt is AnythingLLM's, and deploy never writes one. It sets
the system prompt's block (system-prompt.md, as hostctl.prompt wraps it) as the
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
import urllib.request
from collections import Counter
from collections.abc import Collection
from pathlib import Path

from hostctl import jobs as hostjobs
from hostctl import prompt
from hostctl.units import ROOT, anythingllm_headers, storage

STORAGE = storage()
REPO = ROOT / "anythingllm"
LIVE_SKILLS = STORAGE / "plugins" / "agent-skills"
LIVE_MCP = STORAGE / "plugins" / "anythingllm_mcp_servers.json"
REPO_JOBS = hostjobs.REPO_JOBS
API = os.environ.get("ANYTHINGLLM_API", "http://127.0.0.1:3001/api")
JOB_FIELDS = ("prompt", "tools", "schedule")


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
    return hostjobs.repo_jobs(REPO_JOBS)


def live_jobs(names: Collection[str]) -> dict[str, dict]:
    """The live jobs by name; exits if `names` (the ones about to be matched) has a name
    two live jobs share, since which of them a write would reach is anyone's guess."""
    found = api("GET", "/scheduled-jobs")["jobs"]
    counts = Counter(j["name"] for j in found)
    if twice := [n for n in names if counts[n] > 1]:
        sys.exit(
            f"AnythingLLM has more than one scheduled job named {', '.join(map(repr, twice))}: "
            "delete or rename the extra ones in its UI (Scheduled Jobs), then run this again."
        )
    jobs = {}
    for job in found:
        job["tools"] = hostjobs.job_tools(job) or []
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
    if (live := planned_default()) is not None:
        changed = True
        print_text_diff("system-prompt/default", live.strip(), repo_block())
    if (variable := planned_variable()) is not None:
        changed = True
        live_var, value = variable
        old = live_var["value"] if live_var else "(none)"
        print(f"{{{prompt.VARIABLE}}}: {old} -> {value}")
    wanted = repo_jobs()
    for job, live in planned(wanted, lambda: live_jobs(wanted), JOB_FIELDS):
        changed = True
        print_job_diff(job, live)
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


def deploy() -> None:
    files = {
        d: t for d, t in planned_files().items() if not d.exists() or d.read_text() != t
    }
    wanted = repo_jobs()
    jobs = planned(wanted, lambda: live_jobs(wanted), JOB_FIELDS)
    default = planned_default()
    variable = planned_variable()
    if not files and not jobs and default is None and variable is None:
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
        api("PUT", f"/scheduled-jobs/{live['id']}", body)
        state = "enabled" if live["enabled"] else "disabled"
        print(f"deployed scheduled job {job['name']!r} (id {live['id']}, {state})")
    for dest, text in files.items():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.tmp")
        tmp.write_text(text)
        os.replace(tmp, dest)
        print(f"deployed {dest}")


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
    live = live_jobs([name]).get(name)
    if live is None:
        sys.exit(f"no live scheduled job named {name!r}")
    slug = "-".join("".join(c if c.isalnum() else " " for c in name.lower()).split())
    dest = REPO_JOBS / slug
    meta = {"name": live["name"], "schedule": live["schedule"], "tools": live["tools"]}
    write_repo_dir(dest, "job.json", meta, live["prompt"])
    print(f"imported scheduled job {name!r} -> {dest.relative_to(ROOT)}")


def mcp_packages() -> list[str]:
    """The workspace members the MCP servers run (each one's `--package`), so `uv run hostctl mcp-sync`
    installs what they need into the container's venv and nothing else."""
    servers = json.loads((REPO / "mcp_servers.json").read_text())["mcpServers"]
    names = [
        s["args"][s["args"].index("--package") + 1]
        for s in servers.values()
        if "--package" in s["args"]
    ]
    return list(dict.fromkeys(names))


def main(argv: list[str] | None = None) -> None:
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
    sub.add_parser(
        "mcp-packages", help="print the workspace members the MCP servers run"
    )
    args = parser.parse_args(argv)

    if args.cmd == "mcp-packages":
        print(" ".join(mcp_packages()))
    elif args.cmd == "diff":
        diff()
    elif args.cmd == "deploy":
        deploy()
    elif args.cmd == "import-job":
        import_job(args.name)
    else:
        import_skill(args.name)


if __name__ == "__main__":
    main()
