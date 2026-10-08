import functools
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGES = Path(__file__).resolve().parent
SANDBOX_IMAGE = "localhost/everythingllm-sandbox"  # sandbox.runner.IMAGE
SITEBUILD = PACKAGES / "sandbox" / "src" / "sandbox" / "sitebuild.py"
THEMES = PACKAGES / "sites" / "zola" / "themes"


# Where tests find AnythingLLM: a port nothing listens on, so a test that forgets its fake
# fails rather than reaching this machine's AnythingLLM.
NO_ANYTHINGLLM = "http://127.0.0.1:9"


@pytest.fixture(autouse=True)
def no_host_settings(monkeypatch, tmp_path_factory):
    """Tests run the same on any machine: none of this one's host.env (which `uv run hostctl test`
    exports, and which the site builds would find at the repo root) reaches them, and none
    reaches its AnythingLLM or reads its storage (where its .env, with the password, is)."""
    monkeypatch.delenv("PUBLIC_HOST", raising=False)
    monkeypatch.setenv("ANYTHINGLLM_STORAGE", str(tmp_path_factory.mktemp("storage")))
    monkeypatch.setenv("ANYTHINGLLM_URL", NO_ANYTHINGLLM)
    monkeypatch.setenv("ANYTHINGLLM_API", f"{NO_ANYTHINGLLM}/api")
    monkeypatch.delenv("ANYTHINGLLM_ENV", raising=False)
    # hostctl reads its address on import.
    for name in ("hostctl.sync", "hostctl.machine"):
        if name in sys.modules:
            monkeypatch.setattr(sys.modules[name], "API", f"{NO_ANYTHINGLLM}/api")
    # Nor a service container's: its egress proxy and the addresses it reaches the host by.
    for key in (
        "EGRESS_PROXY",
        "SEARXNG_URL",
        "LIVE_HOST",
        "ARTICLES_HOST",
    ):
        monkeypatch.delenv(key, raising=False)
    # Nor does any test reach this machine's runners (site builds would ask the sandbox's).
    # Set, not just left out, so what a test sets with os.environ.setdefault (the
    # gateway's host_sockets) is undone after it rather than left for the tests after.
    for folder in (
        "sandbox",
        "sandbox-build",
        "sites",
        "agents",
        "research",
        "browser",
    ):
        env = f"{folder.replace('-', '_').upper()}_SOCKET"
        monkeypatch.setenv(env, f"/nonexistent/{folder}/runner.sock")
    try:
        from sites import store
    except ImportError:  # a package that doesn't use the sites
        return
    monkeypatch.setattr(store, "host_file", lambda source: source / "no-host.env")


@functools.cache
def _no_sandbox_image() -> str:
    """Why a real site build can't run here, or "" if it can."""
    if shutil.which("podman") is None:
        return "no podman here"
    found = subprocess.run(
        ["podman", "image", "exists", SANDBOX_IMAGE], capture_output=True, check=False
    )
    if found.returncode != 0:
        return f"no {SANDBOX_IMAGE} here (uv run hostctl sandbox-images)"
    return ""


@pytest.fixture
def sandbox_zola(tmp_path_factory):
    """A real zola build, where the only zola is: in the sandbox image, run as the
    sandbox runner runs a system site's build (no network, read-only, the repo's
    sitebuild.py and themes). `sandbox_zola(site, out, base_url, entries=None)` builds the
    Zola site folder `site` into `out` (made new) and returns the CompletedProcess.
    Skips without podman or the image."""
    if why := _no_sandbox_image():
        pytest.skip(why)

    def build(
        site: Path, out: Path, base_url: str, entries: Path | None = None
    ) -> subprocess.CompletedProcess:
        stage = tmp_path_factory.mktemp("sandbox-out")
        if entries is None or not entries.is_dir():
            entries = tmp_path_factory.mktemp("no-entries")
        done = subprocess.run(
            [
                "podman", "run", "--rm", "--network", "none", "--read-only",
                "--tmpfs", "/tmp", "--userns", "keep-id", "--cap-drop", "ALL",
                "-v", f"{site}:/site:ro",
                "-v", f"{entries}:/entries:ro",
                "-v", f"{THEMES}:/system/themes:ro",
                "-v", f"{stage}:/out",
                "-v", f"{SITEBUILD}:/sandbox/sitebuild.py:ro",
                SANDBOX_IMAGE,
                "python", "/sandbox/sitebuild.py", "/site", base_url, "/entries",
            ],
            capture_output=True, text=True, check=False, timeout=120,
        )  # fmt: skip
        if done.returncode == 0:
            shutil.move(stage / "site", out)
        return done

    return build
