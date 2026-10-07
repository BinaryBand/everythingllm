"""Setting up a machine from this repo; the steps `uv run hostctl install` runs around the others.

  check      before anything changes: host.env, the tools the units run, linger and the
             storage folder (creating what the containers mount inside it)
  wait-api   wait for AnythingLLM's API to answer after its container (re)starts
  search     point AnythingLLM's web search at this machine's SearXNG
  checklist  what's left to do by hand in AnythingLLM's UI, ticking what's already done

Standard library only, like the rest of hostctl. It only ever checks
whether a key in AnythingLLM's .env is set, and reads which agent skills are on and
auto-approved from its database (read-only); it never prints a value.
"""

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from hostctl import appctl, apps
from hostctl.units import ROOT, anythingllm_headers, env_file, host_settings

API = "http://127.0.0.1:3001/api"
EXAMPLE_HOST = "machine.example.net"
# Paths the systemd units run these from (host/systemd/, host/quadlet/).
TOOLS = {
    "podman": "/usr/bin/podman",
    "uv": "/usr/local/bin/uv",
}
# The pages site's folder, which the static_agent container mounts, so it must exist first.
SITE_DIR = Path.home() / ".local" / "share" / "everythingllm" / "pages" / "public"


def settings() -> dict[str, str]:
    return host_settings(ROOT / "host.env")


def run(*cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def check() -> list[str]:
    """Problems that stop an install, each with what to do about it."""
    problems = []
    values = settings()
    if not (ROOT / "host.env").is_file():
        return ["no host.env: copy host.env.example to host.env and fill it in."]
    host, storage = values.get("PUBLIC_HOST", ""), values.get("ANYTHINGLLM_STORAGE", "")
    if not host or host == EXAMPLE_HOST:
        problems.append(
            "PUBLIC_HOST in host.env isn't set to the name this machine is reached by over HTTPS."
        )
    elif fail := appctl.host_problems(host)[0]:
        problems.append(f"PUBLIC_HOST: {fail}.")
    for tool, path in TOOLS.items():
        if not Path(path).is_file():
            problems.append(
                f"{tool} isn't at {path}, where the units run it from: install it there or link it."
            )
    if not shutil.which("podman") or run("podman", "info").returncode:
        problems.append("rootless podman doesn't work for this user (`podman info`).")
    if run("systemctl", "--user", "is-system-running").returncode not in (
        0,
        1,
    ):  # 1: degraded
        problems.append("there's no systemd user session (`systemctl --user status`).")
    if (
        run(
            "loginctl",
            "show-user",
            run("id", "-un").stdout.strip(),
            "-p",
            "Linger",
            "--value",
        ).stdout.strip()
        != "yes"
    ):
        problems.append(
            "lingering is off, so user units won't start at boot: `sudo loginctl enable-linger $USER`."
        )
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    if storage:
        root = Path(storage)
        if not root.is_dir():
            problems.append(
                f"storage folder {root} doesn't exist: `sudo install -d -o $USER -m 2770 {root}`."
            )
        else:
            try:
                env = root / ".env"
                if not env.exists():  # the container mounts it; AnythingLLM fills it in
                    env.touch(mode=0o600)
            except PermissionError:
                problems.append(f"can't write to {root}: `sudo chown -R $USER {root}`.")
    else:
        problems.append("ANYTHINGLLM_STORAGE isn't set in host.env.")
    return problems


def api(method: str, path: str, body: dict | None = None, fresh: bool = False) -> dict:
    """Call AnythingLLM's internal API, logged in if it has a password (once more after a 401)."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if path != "/ping":  # answers before setup, and without a login
        headers |= anythingllm_headers(API, fresh)
    req = urllib.request.Request(API + path, data, headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 401 and not fresh:
            return api(method, path, body, fresh=True)
        raise


def wait_api(timeout: float = 180) -> None:
    end = time.monotonic() + timeout
    while True:
        try:
            if api("GET", "/ping").get("online"):
                return
        except (urllib.error.URLError, OSError, ValueError):
            pass
        if time.monotonic() > end:
            sys.exit(
                f"AnythingLLM's API didn't answer at {API} within {timeout:.0f} s: `uv run hostctl logs`."
            )
        time.sleep(2)


def search() -> None:
    url = f"https://{settings()['PUBLIC_HOST']}:8888/search"
    api("POST", "/system/update-env", {"AgentSearXNGApiUrl": url})
    api(
        "POST", "/admin/system-preferences", {"agent_search_provider": "searxng-engine"}
    )
    print(f"web search: SearXNG at {url}")


def env_keys(storage: Path) -> dict[str, bool]:
    """Which keys AnythingLLM's .env sets to something (values are never kept)."""
    return {k: bool(v) for k, v in env_file(storage / ".env").items()}


def reranker_off(storage: Path) -> bool:
    """Whether AnythingLLM gives the agent every tool (anythingllm/env.example says why)."""
    return env_file(storage / ".env").get("AGENT_SKILL_RERANKER_ENABLED") == "false"


# AnythingLLM's own tools that make a job or write a file: a delegated task can reach them
# (our skills refuse it; these don't), so none may run without asking, and the job tool
# is off for schedule-job, which refuses it (README, "Delegation").
JOB_TOOL = "create-scheduled-job"
ASK_FIRST = (JOB_TOOL, "filesystem-write-text-file", "filesystem-edit-file")


def agent_skills(storage: Path) -> dict[str, list[str]] | None:
    """AnythingLLM's built-in skills that are on and that run without asking, read-only
    from its database; None when it can't be read."""
    db = storage / "anythingllm.db"
    if not db.is_file():
        return None
    labels = {"default_agent_skills": "on", "whitelisted_agent_skills": "auto"}
    try:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT label, value FROM system_settings WHERE label IN (?, ?)",
                tuple(labels),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    found: dict[str, list[str]] = {"on": [], "auto": []}
    for label, value in rows:
        try:
            listed = json.loads(value or "[]")
        except ValueError:
            return None
        found[labels[label]] = (
            [str(x) for x in listed] if isinstance(listed, list) else []
        )
    return found


def jobs_kept_to_chats(storage: Path) -> bool | None:
    """Whether create-scheduled-job is off and no job or file tool runs without asking."""
    skills = agent_skills(storage)
    if skills is None:
        return None
    return JOB_TOOL not in skills["on"] and not set(ASK_FIRST) & set(skills["auto"])


def searxng_answers() -> bool:
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8888/search?q=test&format=json", timeout=10
        ) as resp:
            return "results" in json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def routed(host: str) -> bool:
    """Whether `uv run hostctl routes` passes on `host`."""
    return bool(host) and appctl.route_report(apps.load(), host)[1]


def checklist() -> list[tuple[bool | None, str]]:
    """(done, item): done is None for what can't be checked from here."""
    values = settings()
    keys = env_keys(Path(values["ANYTHINGLLM_STORAGE"]))
    try:
        workspaces = len(api("GET", "/workspaces")["workspaces"])
    except (urllib.error.URLError, OSError, KeyError, ValueError):
        workspaces = 0
    return [
        (
            keys.get("AUTH_TOKEN", False) and keys.get("JWT_SECRET", False),
            "Set a password (Settings > Security > Password protection): without one, anyone who reaches :3001 can use AnythingLLM's own API, scheduled jobs included. Use a long random one; our tools log in with it from the .env.",
        ),
        (
            keys.get("LLM_PROVIDER", False),
            "Choose the chat model and enter its key (Settings > LLM Preference).",
        ),
        (
            keys.get("EMBEDDING_ENGINE", False),
            "Choose the embedder (Settings > Embedder); for Ollama, it has to be reachable from the container.",
        ),
        (
            keys.get("DEEPSEEK_API_KEY", False),
            "Enter a DeepSeek key (Settings > LLM Preference > DeepSeek): the deep-research runner and the article writer use it even when chat runs on another model.",
        ),
        (
            keys.get("GENERIC_OPEN_AI_API_KEY", False)
            or keys.get("ZAI_API_KEY", False),
            "Enter a Z.AI key, for the deep-research planner (glm-5.3): as the Generic OpenAI provider with base URL https://api.z.ai/api/coding/paas/v4, or on the Z.AI provider's page.",
        ),
        (
            reranker_off(Path(values["ANYTHINGLLM_STORAGE"]))
            and keys.get("AGENT_MAX_TOOL_CALLS", False),
            "Add AGENT_SKILL_RERANKER_ENABLED=false and AGENT_MAX_TOOL_CALLS to the .env (see anythingllm/env.example for why), then `uv run hostctl restart`.",
        ),
        (
            jobs_kept_to_chats(Path(values["ANYTHINGLLM_STORAGE"])),
            "Turn off create-scheduled-job, and take create-scheduled-job, filesystem-write-text-file and filesystem-edit-file off the tools that run without asking (Agent Skills page): a delegated task can reach AnythingLLM's own tools, and a job runs with every tool approved. schedule-job makes recurring jobs instead.",
        ),
        (
            routed(values.get("PUBLIC_HOST", "")),
            "Route the apps' HTTPS ports on PUBLIC_HOST to their servers on 127.0.0.1, from this machine (tailscale serve, Caddy, …), keeping them to your own devices: `uv run hostctl routes` lists them and checks each.",
        ),
        (
            searxng_answers(),
            "Run SearXNG on 127.0.0.1:8888 (this repo doesn't deploy it; see the README's SearXNG section).",
        ),
        (
            workspaces > 0,
            "Create a workspace (it starts with the system prompt, as the default for new workspaces).",
        ),
        (
            None,
            "Turn off built-in agent skills nobody uses (Agent Skills page), so the reranker keeps this setup's tools in range.",
        ),
        (
            None,
            "Connect Gmail (Agent Skills page) if you want the email tools the system prompt describes.",
        ),
    ]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=["check", "wait-api", "search", "checklist"])
    args = parser.parse_args(argv)
    if args.action == "check":
        problems = check()
        for p in problems:
            print(f"  - {p}")
        if problems:
            sys.exit("Fix these, then run `uv run hostctl install` again.")
        print("machine check: ready")
    elif args.action == "wait-api":
        wait_api()
    elif args.action == "search":
        search()
    else:
        items = checklist()
        print("\nLeft to do in AnythingLLM (uv run hostctl install sets up everything else):")
        for done, item in items:
            print(f"  [{'x' if done else ' ' if done is not None else '?'}] {item}")
        print(
            "[?]: can't be checked from here. Run `uv run hostctl install` again any time; it only changes what's out of date."
        )


if __name__ == "__main__":
    main()
