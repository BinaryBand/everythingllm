"""Setting up a machine from this repo; the steps `make install` runs around the others.

  check      before anything changes: host.env, the tools the units run, linger, tailscale,
             and the storage folder (creating what the containers mount inside it)
  wait-api   wait for AnythingLLM's API to answer after its container (re)starts
  search     point AnythingLLM's web search at this machine's SearXNG
  checklist  what's left to do by hand in AnythingLLM's UI, ticking what's already done

Standard library only, run with the system `python3`, like sync.py. It only ever checks
whether a key in AnythingLLM's .env is set; it never prints a value.
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from units import ROOT, anythingllm_headers, env_file, host_settings

API = "http://127.0.0.1:3001/api"
EXAMPLE_HOST = "machine.tailnet-name.ts.net"
# Paths the systemd units run these from (host/systemd/, host/quadlet/).
TOOLS = {
    "podman": "/usr/bin/podman",
    "uv": "/usr/local/bin/uv",
    "zola": "/usr/local/bin/zola",
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
            "PUBLIC_HOST in host.env isn't set to this machine's tailnet name (`tailscale status --self`)."
        )
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
    if not shutil.which("tailscale") or run("tailscale", "status").returncode:
        problems.append("tailscale isn't installed or isn't up (`tailscale status`).")
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
                f"AnythingLLM's API didn't answer at {API} within {timeout:.0f} s: `make logs`."
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


def searxng_answers() -> bool:
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8888/search?q=test&format=json", timeout=10
        ) as resp:
            return "results" in json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return False


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
            "Set a password (Settings > Security > Password protection): without one, anyone who reaches :3001 on the tailnet can use AnythingLLM's own API, scheduled jobs included. Use a long random one; our tools log in with it from the .env.",
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
            keys.get("AGENT_SKILL_RERANKER_TOP_N", False)
            and keys.get("AGENT_MAX_TOOL_CALLS", False),
            "Add AGENT_SKILL_RERANKER_TOP_N and AGENT_MAX_TOOL_CALLS to the .env (see anythingllm/env.example for why), then `make restart`.",
        ),
        (
            searxng_answers(),
            "Run SearXNG on 127.0.0.1:8888 (this repo doesn't deploy it; see the README's SearXNG section).",
        ),
        (
            workspaces > 0,
            "Create a workspace, then run `make deploy` again so it gets the system prompt.",
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=["check", "wait-api", "search", "checklist"])
    args = parser.parse_args()
    if args.action == "check":
        problems = check()
        for p in problems:
            print(f"  - {p}")
        if problems:
            sys.exit("Fix these, then run `make install` again.")
        print("machine check: ready")
    elif args.action == "wait-api":
        wait_api()
    elif args.action == "search":
        search()
    else:
        items = checklist()
        print("\nLeft to do in AnythingLLM (make install sets up everything else):")
        for done, item in items:
            print(f"  [{'x' if done else ' ' if done is not None else '?'}] {item}")
        print(
            "[?]: can't be checked from here. Run `make install` again any time; it only changes what's out of date."
        )


if __name__ == "__main__":
    main()
