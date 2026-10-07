import functools
import shutil
import subprocess
from pathlib import Path

import pytest

PACKAGES = Path(__file__).resolve().parent
SANDBOX_IMAGE = "localhost/everythingllm-sandbox"  # sandbox.runner.IMAGE
SITEBUILD = PACKAGES / "sandbox" / "src" / "sandbox" / "sitebuild.py"
THEMES = PACKAGES / "sites" / "zola" / "themes"


@pytest.fixture(autouse=True)
def no_host_settings(monkeypatch):
    """Tests run the same on any machine: none of this one's host.env (which `uv run hostctl test`
    exports, and which the site builds would find at the repo root) reaches them."""
    monkeypatch.delenv("PUBLIC_HOST", raising=False)
    monkeypatch.delenv("ANYTHINGLLM_STORAGE", raising=False)
    # Nor a service container's: its egress proxy and the addresses it reaches the host by.
    for key in (
        "EGRESS_PROXY",
        "SEARXNG_URL",
        "ANYTHINGLLM_API",
        "LIVE_HOST",
        "ARTICLES_HOST",
    ):
        monkeypatch.delenv(key, raising=False)
    # Nor does any test reach this machine's sandbox runner (site builds would ask it).
    monkeypatch.setenv("SANDBOX_SOCKET", "/nonexistent/sandbox/runner.sock")
    monkeypatch.setenv("SANDBOX_BUILD_SOCKET", "/nonexistent/sandbox-build/runner.sock")
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
