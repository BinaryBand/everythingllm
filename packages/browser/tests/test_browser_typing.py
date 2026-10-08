"""The user's typing through the take-over view's field (browser.driver's user_type and
user_key), in a real Chromium: the browser image's, run with the repo's driver code on the
path (packages/browser/src and packages/hostrpc/src alone, as in a workspace's container),
headless, with no network. A page on login.example (the probe's own server, which
Chromium resolves to loopback) takes a username and a password typed as text; what went
into the password field is hidden from the agent's read once it has the browser back,
and the form sent with Enter is offered for saving (capture.js). Skips without podman or
the image (`uv run hostctl browser-images`).
"""

import functools
import json
import shutil
import subprocess
from pathlib import Path

import pytest

PACKAGES = Path(__file__).resolve().parents[2]
BROWSER_IMAGE = "localhost/everythingllm-browser"


@functools.cache
def _missing() -> str:
    """Why the test can't run here, or "" if it can."""
    if shutil.which("podman") is None:
        return "no podman here"
    found = subprocess.run(
        ["podman", "image", "exists", BROWSER_IMAGE], capture_output=True, check=False
    )
    return f"no {BROWSER_IMAGE} here" if found.returncode else ""


pytestmark = [
    pytest.mark.skipif(bool(_missing()), reason=_missing() or "ok"),
    pytest.mark.xdist_group("podman"),
]

PROBE = r'''
import asyncio, json, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path[:0] = ["/packages/browser/src", "/packages/hostrpc/src"]
from browser import driver
from playwright.async_api import async_playwright

# Signed in by its script, as the driver blocks a password sent in the clear (http here).
PAGE = b"""<!doctype html><title>Log in</title>
<form onsubmit="event.preventDefault(); location.href = '/done'">
  <input id="user" name="user" autocomplete="username">
  <input id="pass" name="pass" type="password">
  <button>Log in</button>
</form>
<p id="echo"></p>
<script>
  document.getElementById("pass").addEventListener("input", (e) => {
    document.getElementById("echo").textContent = "You typed " + e.target.value;
  });
</script>"""
SECRET = "s3cret-pass-42"


class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        self.answer(PAGE if self.path == "/" else b"<title>Done</title><p>Logged in</p>")


    def answer(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


async def main():
    threading.Thread(target=HTTPServer(("127.0.0.1", 80), Site).serve_forever, daemon=True).start()
    out = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            channel="chromium",  # the image has the full Chromium, no headless shell
            args=["--host-resolver-rules=MAP login.example 127.0.0.1"],
        )
        context = await browser.new_context()
        d = driver.Driver(context, Path("/tmp"))
        await context.expose_binding("__bwCapture", d.on_capture)
        await context.add_init_script(script=driver.CAPTURE)
        await d.op_open("t1", "http://login.example/")
        page = d.current("t1")
        try:
            await d.op_user_type("refused", thread="t1")
        except Exception as e:
            out["agents_turn"] = str(e)
        await d.op_capture(True, user=True)  # the user takes it in the view
        await page.focus("#user")  # their tap on the field, over VNC
        await d.op_user_type("alice@example.com", thread="t1")
        await d.op_user_key("Tab", thread="t1")
        out["focused"] = await page.evaluate("document.activeElement.id")
        await d.op_user_type(SECRET, thread="t1")
        out["user"] = await page.input_value("#user")
        out["password"] = await page.input_value("#pass")
        out["echo"] = await page.inner_text("#echo")  # the page's input event fired
        await d.op_capture(False)  # handed back
        view = await d.op_read("t1")
        out["read"] = json.dumps(view, ensure_ascii=False)
        out["locked"] = d.locked
        await d.op_capture(True, user=True)
        await page.focus("#pass")
        await d.op_user_key("Enter", thread="t1")
        await page.wait_for_url("**/done", timeout=5000)
        await asyncio.sleep(0.3)
        offers = await d.op_offers()
        out["offers"] = offers
        out["offer"] = await d.op_peek_offer(offers[0]["id"]) if offers else None
        out["url"] = page.url
        await browser.close()
    print(json.dumps(out))


asyncio.run(main())
'''


@pytest.fixture(scope="module")
def typed(tmp_path_factory) -> dict:
    """What the probe saw."""
    probe = tmp_path_factory.mktemp("typing") / "probe.py"
    probe.write_text(PROBE)
    done = subprocess.run(
        [
            "podman", "run", "--rm", "--network", "none", "--pull", "never",
            "--entrypoint", "python3",
            "-v", f"{probe}:/probe.py:ro",
            "-v", f"{PACKAGES / 'browser' / 'src'}:/packages/browser/src:ro",
            "-v", f"{PACKAGES / 'hostrpc' / 'src'}:/packages/hostrpc/src:ro",
            BROWSER_IMAGE, "/probe.py",
        ],
        capture_output=True, text=True, check=False, timeout=120,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_text_goes_into_the_focused_field_as_text_and_the_keys_move_on(typed):
    assert typed["user"] == "alice@example.com"
    assert typed["focused"] == "pass"  # Tab
    assert typed["password"] == "s3cret-pass-42"
    assert typed["echo"] == "You typed s3cret-pass-42"


def test_only_while_the_user_has_the_browser(typed):
    assert "only the user types here" in typed["agents_turn"]


def test_what_went_into_the_password_field_is_hidden_from_the_agents_read(typed):
    assert "s3cret" not in typed["read"] and "pass-42" not in typed["read"]
    assert "You typed •••" in typed["read"]  # the page showing it
    assert 'input[password] \\"pass\\" (filled)' in typed["read"]
    assert not typed["locked"]  # the user's text isn't the agent's, checked and refused


def test_the_form_sent_with_enter_is_offered_for_saving(typed):
    assert typed["url"] == "http://login.example/done"
    assert typed["offers"] == [
        {
            "id": typed["offers"][0]["id"],
            "site": "login.example",
            "username": "alice@example.com",
        }
    ]
    assert typed["offer"]["password"] == "s3cret-pass-42"
