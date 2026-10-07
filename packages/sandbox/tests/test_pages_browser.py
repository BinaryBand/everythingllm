"""The workspace pages site's CSP sandbox, in a real browser.

Caddy serves fixture pages with the repo's pages.Caddyfile, and Playwright's Chromium (the
browser image's) loads them, in a pod with no network and no published ports. It holds the
:8447 policy to what the runner's notices tell the agent: inline and same-site scripts run,
in an opaque origin with no storage, no reading the site's files, no forms, no popups and
no alerts; scripts from other hosts don't load; downloads and the directory listing work.
Skips without podman, or without either image (`uv run hostctl browser-images`).
"""

import functools
import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
CADDY_IMAGE = "docker.io/library/caddy:2-alpine"
BROWSER_IMAGE = "localhost/everythingllm-browser"


@functools.cache
def _missing() -> str:
    """Why the browser test can't run here, or "" if it can."""
    if shutil.which("podman") is None:
        return "no podman here"
    for image in (CADDY_IMAGE, BROWSER_IMAGE):
        found = subprocess.run(
            ["podman", "image", "exists", image], capture_output=True, check=False
        )
        if found.returncode != 0:
            return f"no {image} here"
    return ""


pytestmark = pytest.mark.skipif(bool(_missing()), reason=_missing() or "ok")

PAGE = """<!doctype html>
<title>App</title>
<script>
  window.violations = [];
  document.addEventListener("securitypolicyviolation", (e) =>
    window.violations.push(e.violatedDirective + " " + e.blockedURI));
  window.inline = true;
</script>
<script src="same.js"></script>
<script src="https://cdn.example/lib.js"></script>
<script src="//cdn.example/lib2.js"></script>
<script type="module" src="mod.js"></script>
<script type="module">window.inlineModule = true;</script>
<button id="clicked" onclick="window.handler = true">handler</button>
<form id="form" action="sink.html" onsubmit="window.submitted = true">
  <input name="q" value="x"><button id="go">go</button>
</form>
<a id="newtab" href="other.html" target="_blank">new tab</a>
<a id="download" href="data.csv" download>csv</a>
"""

PROBE = """
import json, sys, time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8447/ws/"
out = {}
with sync_playwright() as p:
    # The image has Playwright's full Chromium and no headless shell.
    browser = p.chromium.launch(headless=True, channel="chromium")
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    dialogs, popups = [], []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    context.on("page", lambda pg: popups.append(pg.url))
    for _ in range(100):  # Caddy may still be starting
        try:
            response = page.goto(BASE + "app/")
            break
        except Exception:
            time.sleep(0.1)
    else:
        sys.exit("caddy never answered")
    out["csp"] = response.headers.get("content-security-policy")
    page.wait_for_timeout(300)  # module scripts run after the page has loaded
    out.update(page.evaluate('''async () => {
        const r = {inline: !!window.inline, same: !!window.same,
                   module_src: !!window.mod, inline_module: !!window.inlineModule,
                   origin: self.origin};
        for (const [name, f] of [["localStorage", () => localStorage.getItem("x")],
                                 ["sessionStorage", () => sessionStorage.getItem("x")],
                                 ["cookie", () => document.cookie]]) {
            try { f(); r[name] = "works"; } catch (e) { r[name] = e.name; }
        }
        try { r.fetch = await (await fetch("data.csv")).text(); }
        catch (e) { r.fetch = e.name; }
        r.window_open = window.open("other.html");
        alert("hi");
        return r;
    }'''))
    page.click("#clicked")
    out["handler"] = page.evaluate("!!window.handler")
    page.click("#newtab")
    page.click("#go")
    page.evaluate("HTMLFormElement.prototype.submit.call(document.getElementById('form'))")
    page.wait_for_timeout(500)
    out["submitted"] = page.evaluate("!!window.submitted")
    out["url"] = page.url
    out["violations"] = page.evaluate("window.violations")
    with page.expect_download(timeout=5000) as download:
        page.click("#download")
    with open(download.value.path()) as f:
        out["download"] = f.read()
    out["dialogs"], out["popups"] = dialogs, list(popups)
    listing = context.new_page()
    errors = []
    listing.on("pageerror", lambda e: errors.append(str(e)))
    listing.goto(BASE)
    out["listing"] = listing.inner_text("body")
    out["listing_errors"] = errors
    browser.close()
print(json.dumps(out))
"""


def podman(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["podman", *args], capture_output=True, text=True, check=False, timeout=timeout
    )


@pytest.fixture(scope="module")
def browsed(tmp_path_factory) -> dict:
    """What the probe saw on the fixture pages, served by the repo's Caddyfile."""
    tmp = tmp_path_factory.mktemp("pages-browser")
    app = tmp / "workspaces" / "ws" / "app"
    app.mkdir(parents=True)
    (app / "index.html").write_text(PAGE)
    (app / "same.js").write_text("window.same = true;\n")
    (app / "mod.js").write_text("window.mod = true;\n")
    (app / "other.html").write_text("<title>Other</title>")
    (app / "sink.html").write_text("<title>Sink</title>")
    (app / "data.csv").write_text("a,b\n1,2\n")
    (tmp / "probe.py").write_text(PROBE)
    pod = f"pages-csp-test-{uuid.uuid4().hex[:8]}"
    caddyfile = REPO / "host" / "caddy" / "pages.Caddyfile"
    try:
        made = podman("pod", "create", "--name", pod, "--network", "none")
        assert made.returncode == 0, made.stderr
        caddy = podman(
            "run", "-d", "--rm", "--pod", pod, "--pull", "never",
            "-v", f"{caddyfile}:/etc/caddy/Caddyfile:ro",
            "-v", f"{tmp / 'workspaces'}:/srv/workspaces:ro",
            CADDY_IMAGE,
        )  # fmt: skip
        assert caddy.returncode == 0, caddy.stderr
        probe = podman(
            "run", "--rm", "--pod", pod, "--pull", "never",
            "--entrypoint", "python3",
            "-v", f"{tmp / 'probe.py'}:/probe.py:ro",
            BROWSER_IMAGE, "/probe.py",
            timeout=90,
        )  # fmt: skip
        assert probe.returncode == 0, probe.stderr[-3000:]
        return json.loads(probe.stdout.strip().splitlines()[-1])
    finally:
        podman("pod", "rm", "-f", "--time", "0", pod)


def test_the_header_is_the_sandboxed_policy(browsed):
    assert browsed["csp"].endswith("; sandbox allow-scripts allow-downloads")


def test_inline_and_same_site_scripts_run_in_an_opaque_origin(browsed):
    # 'self' in script-src still matches the site's own scripts in a sandboxed page.
    assert browsed["inline"] and browsed["same"] and browsed["handler"]
    assert browsed["inline_module"]
    assert browsed["origin"] == "null"


def test_storage_and_reading_the_sites_files_fail(browsed):
    assert browsed["localStorage"] == "SecurityError"
    assert browsed["sessionStorage"] == "SecurityError"
    assert browsed["cookie"] == "SecurityError"
    assert browsed["fetch"] == "TypeError"  # the same host, but a cross-origin read
    assert not browsed["module_src"]  # fetched with CORS too


def test_no_forms_popups_new_tabs_or_alerts(browsed):
    assert not browsed["submitted"]  # the submit event never even fires
    assert browsed["url"] == "http://127.0.0.1:8447/ws/app/"
    assert browsed["window_open"] is None
    assert browsed["popups"] == []  # neither window.open nor target=_blank
    assert browsed["dialogs"] == []


def test_scripts_from_other_hosts_are_blocked(browsed):
    assert browsed["violations"] == [
        "script-src-elem https://cdn.example/lib.js",
        "script-src-elem http://cdn.example/lib2.js",
    ]


def test_downloads_and_the_directory_listing_work(browsed):
    assert browsed["download"] == "a,b\n1,2\n"
    assert "app/" in browsed["listing"]
    assert browsed["listing_errors"] == []
