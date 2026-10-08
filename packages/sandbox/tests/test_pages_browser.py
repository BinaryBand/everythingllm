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


pytestmark = [
    pytest.mark.skipif(bool(_missing()), reason=_missing() or "ok"),
    # One worker for the module's pod: under xdist each worker would start its own.
    pytest.mark.xdist_group("podman"),
]

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

APP_PAGE = """<!doctype html>
<title>An app</title>
<script>
  fetch("/_apps/ws/x/ops", {method: "POST", headers: {"Content-Type": "text/plain"},
                            body: JSON.stringify({token: "t", op: "check", args: {item: 1}})})
    .then((r) => r.json()).then((j) => { window.posted = j.ok ? "read" : "unread"; })
    .catch((e) => { window.posted = e.name; });
</script>
"""

PROBE = """
import json, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8447/ws/"
out = {}
seen = []

class Apps(BaseHTTPRequestHandler):
    # The sandbox runner's write-back, as the machine routes /_apps/ to it.
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        seen.append([self.path, self.headers.get("Origin"), self.headers.get("Content-Type"),
                     json.loads(body)])
        reply = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "null")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)
    def log_message(self, *a):
        pass

threading.Thread(target=HTTPServer(("127.0.0.1", 8455), Apps).serve_forever, daemon=True).start()
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
    app = context.new_page()
    response = app.goto(BASE + "apps/x/")
    out["app_cache"] = response.headers.get("cache-control")
    app.wait_for_function("window.posted !== undefined", timeout=5000)
    out["app_posted"] = app.evaluate("window.posted")
    out["app_seen"] = seen
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
    (tmp / "workspaces" / "ws" / "apps" / "x").mkdir(parents=True)
    (tmp / "workspaces" / "ws" / "apps" / "x" / "index.html").write_text(APP_PAGE)
    (tmp / "probe.py").write_text(PROBE)
    pod = f"pages-csp-test-{uuid.uuid4().hex[:8]}"
    # The repo's Caddyfile, with /_apps/ sent where the machine's route would send it.
    caddyfile = tmp / "Caddyfile"
    caddyfile.write_text(
        (REPO / "host" / "caddy" / "pages.Caddyfile")
        .read_text()
        .replace(":8447 {", ":8447 {\n  reverse_proxy /_apps/* 127.0.0.1:8455", 1)
    )
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


def test_an_app_page_writes_back_to_its_own_host_and_reads_the_answer(browsed):
    """The sandbox runner's write-back (sandbox.appsweb) answers a page's opaque origin."""
    assert browsed["app_posted"] == "read"
    [(path, origin, kind, body)] = browsed["app_seen"]
    assert (path, origin, kind) == ("/_apps/ws/x/ops", "null", "text/plain")
    assert body == {"token": "t", "op": "check", "args": {"item": 1}}
    assert browsed["app_cache"] == "no-cache"
