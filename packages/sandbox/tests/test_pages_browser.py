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
from sandbox import apps

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

FORM_APP = """<!doctype html>
<title>A form</title>
<form id="f"><input id="field" name="q"><button id="add">Add</button></form>
<script src="app.js"></script>
<script>
  App.start(/*APP*/null);
  window.submits = [];
  document.getElementById("f").onsubmit = (e) => {
    e.preventDefault();
    window.submits.push(document.getElementById("field").value);
  };
</script>
"""

# A page whose colours follow the device's theme, with the client that can keep it to another.
THEMED = """<!doctype html>
<title>Themed</title>
<style>
body{background:rgb(255, 255, 255)}
@media (prefers-color-scheme: dark){body{background:rgb(0, 0, 0)}}
@media screen and (prefers-color-scheme: light){body{color:rgb(1, 2, 3)}}
</style>
<script src="app.js"></script>
"""

PROBE = """
import json, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8447/ws/"
out = {}
seen = []

# The app after another tab's change, which the page's sync is given once it has saved its own.
ELSEWHERE = {"title": "Groceries", "version": 3, "card": {"items": "/data/items"},
             "data": {"items": [{"text": "Oat milk", "done": True}, {"text": "Rye", "done": False}]}}

class Apps(BaseHTTPRequestHandler):
    # The sandbox runner's write-back (sandbox.appsweb), as the machine routes /_apps/ to it,
    # played as a script: the page's patch takes the app to version 2, then another tab's
    # to 3.
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append([self.path, self.headers.get("Origin"), self.headers.get("Content-Type"), body])
        if self.path.endswith("/ops"):
            answer = {"version": 2}
        elif body.get("since") == 2:
            answer = ELSEWHERE
        else:
            time.sleep(0.3)
            answer = {"version": body.get("since")}
        reply = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "null")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)
    def log_message(self, *a):
        pass

threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", 8455), Apps).serve_forever, daemon=True).start()
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
    app_errors = []
    app.on("pageerror", lambda e: app_errors.append(str(e)))
    response = app.goto(BASE + "apps/x/")
    out["app_cache"] = response.headers.get("cache-control")
    out["app_first"] = app.inner_text("#items")
    app.click("text=Oat milk")
    app.wait_for_selector("#items >> text=Rye", timeout=8000)
    out["app_after"] = app.inner_text("#items")
    out["app_checked"] = app.evaluate("[...document.querySelectorAll('#items input')].map((b) => b.checked)")
    out["app_status"] = app.inner_text("#status")
    out["app_seen"] = seen
    out["app_errors"] = app_errors
    form = context.new_page()
    form.goto(BASE + "apps/f/")
    form.fill("#field", "by enter")
    form.press("#field", "Enter")
    form.fill("#field", "by click")
    form.click("#add")
    form.wait_for_timeout(300)
    out["form_submits"] = form.evaluate("window.submits")
    out["form_url"] = form.url
    out["themes"] = {}
    for device in ("light", "dark"):
        themed = browser.new_context(color_scheme=device)
        for asked in ("", "light", "dark"):
            shown = themed.new_page()
            shown.goto(BASE + "apps/t/" + (f"?theme={asked}" if asked else ""))
            out["themes"][f"{device}/{asked or '-'}"] = shown.evaluate(
                "[getComputedStyle(document.body).backgroundColor, "
                "getComputedStyle(document.body).color, "
                "getComputedStyle(document.documentElement).colorScheme]")
        themed.close()
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
    # The list starter as the runner renders it, with app.js beside it.
    x = tmp / "workspaces" / "ws" / "apps" / "x"
    x.mkdir(parents=True)
    groceries = apps.new("Groceries", "list")
    groceries["version"] = 1
    groceries["data"]["items"] = [{"text": "Oat milk", "done": False}]
    page = apps.render(
        apps.starter_page("list").decode(),
        groceries,
        "t",
        "/_apps/ws/x/ops",
        "/_apps/ws/x/sync",
    )
    (x / "index.html").write_text(page)
    (x / "app.js").write_bytes(apps.CLIENT.read_bytes())
    f = tmp / "workspaces" / "ws" / "apps" / "f"
    f.mkdir(parents=True)
    page = apps.render(FORM_APP, groceries, "t", "/_apps/ws/f/ops", "/_apps/ws/f/sync")
    (f / "index.html").write_text(page)
    (f / "app.js").write_bytes(apps.CLIENT.read_bytes())
    t = tmp / "workspaces" / "ws" / "apps" / "t"
    t.mkdir(parents=True)
    (t / "index.html").write_text(apps.inline_client(THEMED))
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


def test_an_app_page_saves_through_app_js_and_sees_other_changes(browsed):
    """The list starter on the client (apps/client.js) against the runner's write-back
    (sandbox.appsweb), which answers a page's opaque origin."""
    assert browsed["app_errors"] == []
    assert browsed["app_first"] == "Oat milk"
    writes = [b for path, _, _, b in browsed["app_seen"] if path.endswith("/ops")]
    assert writes == [
        {
            "token": "t",
            "patch": [
                {"op": "test", "path": "/items/0/text", "value": "Oat milk"},
                {"op": "replace", "path": "/items/0/done", "value": True},
            ],
            "version": 1,
        }
    ]
    for path, origin, kind, body in browsed["app_seen"]:
        assert path in ("/_apps/ws/x/ops", "/_apps/ws/x/sync", "/_apps/ws/f/sync")
        assert (origin, kind, body["token"]) == ("null", "text/plain", "t")
    # Another tab's change came in by sync, after the page's own was saved.
    assert browsed["app_after"].split("\n") == ["Rye", "Oat milk"]
    assert browsed["app_checked"] == [False, True]
    assert browsed["app_cache"] == "no-cache"


def test_app_js_gives_a_form_its_submit_event_without_submitting_it(browsed):
    """The sandbox has no allow-forms, so a form never fires submit; app.js fires one."""
    assert browsed["form_submits"] == ["by enter", "by click"]
    assert browsed["form_url"] == "http://127.0.0.1:8447/ws/apps/f/"


def test_app_js_keeps_a_page_to_the_theme_it_was_asked_for(browsed):
    """The chat app opens a page in its own theme (?theme=), which may not be the device's."""
    white, black, ink = "rgb(255, 255, 255)", "rgb(0, 0, 0)", "rgb(1, 2, 3)"
    themes = browsed["themes"]
    # Asked for nothing, the page follows the device.
    assert themes["light/-"][:2] == [white, ink]
    assert themes["dark/-"][0] == black and themes["dark/-"][1] != ink
    for device in ("light", "dark"):
        assert themes[f"{device}/light"] == [white, ink, "light"]
        background, color, scheme = themes[f"{device}/dark"]
        assert (background, scheme) == (black, "dark") and color != ink
