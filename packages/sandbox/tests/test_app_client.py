"""app.js (sandbox/apps/client.js), the app page's client, under node with a fake host.

What an agent's page relies on without reading the client: App.start returns the data and
it stays the one live object, a path is in the data unless it's /title or /card, onchange
runs after every change, a patch is a promise of whether it saved, and a conflict is
rebased or dropped. The pages in a real browser are test_pages_browser.py's.
"""

import json
import shutil
import subprocess

import pytest
from sandbox import apps

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="no node here")

HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const posts = [];
let answers = [];  // what the fake host answers each write, in turn
const ctx = {
  console, setTimeout, clearTimeout, Promise, JSON,
  document: { visibilityState: "hidden", addEventListener() {} },
  fetch: async (url, opts) => {
    const body = JSON.parse(opts.body);
    posts.push([url, body]);
    const [status, reply] = url === "/sync" ? [200, { version: body.since }] : answers.shift();
    return { status, json: async () => reply };
  },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8") + "\nthis.App = App;", ctx);
const App = ctx.App;
const out = {};
const tick = () => new Promise((go) => setTimeout(go, 400));
(async () => {
  const data = App.start({ title: "N", version: 2, card: {}, data: { notes: [] },
                          token: "t", ops: "/ops", sync: "/sync" });
  const notes = data.notes;
  let changes = 0;
  App.onchange((d) => { changes += d === App.data ? 1 : 100; });

  answers.push([200, { version: 3 }]);
  const saved = App.patch([{ op: "add", path: "/notes/-", value: { text: "a" } }]);
  App.set("/title", "Notes");
  out.live = data === App.data && notes === App.data.notes && notes.length === 1;
  out.title = App.title;
  out.saved = await saved;
  out.changes = changes;
  out.first_post = posts.filter(([u]) => u === "/ops")[0][1];

  try { App.patch([{ op: "remove", path: "/notes/5" }]); } catch (e) { out.refused = e.message; }

  // Saved elsewhere meanwhile: this page's change goes again on top of the app as it is.
  answers.push([409, { conflict: true, title: "Notes", version: 4, card: {},
                       data: { notes: [{ text: "a" }, { text: "theirs" }] } }]);
  answers.push([200, { version: 5 }]);
  const rebased = App.patch([{ op: "add", path: "/notes/-", value: { text: "mine" } }]);
  out.rebased = await rebased;
  out.after_rebase = JSON.stringify(App.data.notes.map((n) => n.text));
  out.rebase_post = posts.filter(([u]) => u === "/ops").pop()[1].version;

  // A change that no longer fits after one elsewhere is dropped.
  answers.push([409, { conflict: true, title: "Notes", version: 6, card: {}, data: { notes: [] } }]);
  const dropped = App.patch([{ op: "test", path: "/notes/0/text", value: "a" },
                             { op: "remove", path: "/notes/0" }]);
  out.dropped = await dropped;
  out.after_drop = JSON.stringify(App.data);
  out.version = App.version;

  // One that changes an item by its place with no test of it isn't made again either, even
  // where it would apply: the item at that place may be another now.
  App.patch([{ op: "add", path: "/notes/-", value: { text: "b" } }]);
  answers.push([200, { version: 7 }]);
  await tick();
  answers.push([409, { conflict: true, title: "Notes", version: 8, card: {},
                       data: { notes: [{ text: "new" }, { text: "b" }] } }]);
  out.by_place = await App.patch([{ op: "remove", path: "/notes/0" }]);
  out.after_by_place = JSON.stringify(App.data.notes.map((n) => n.text));
  answers.push([409, { conflict: true, title: "Notes", version: 9, card: {},
                       data: { notes: [{ text: "newer" }, { text: "new" }, { text: "b" }] } }]);
  answers.push([200, { version: 10 }]);
  out.guarded = await App.patch([{ op: "test", path: "/notes/1/text", value: "b" },
                                 { op: "remove", path: "/notes/1" }]);
  out.after_guarded = JSON.stringify(App.data.notes.map((n) => n.text));
  console.log(JSON.stringify(out));
})();
"""


def test_app_js_keeps_what_a_page_relies_on(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS)
    done = subprocess.run(
        ["node", str(harness), str(apps.CLIENT)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out["live"] and out["title"] == "Notes" and out["saved"] is True
    assert out["changes"] == 2  # its own two changes, with App.data
    assert out["first_post"] == {
        "token": "t",
        "patch": [
            {"op": "add", "path": "/notes/-", "value": {"text": "a"}},
            {"op": "replace", "path": "/title", "value": "Notes"},
        ],
        "version": 2,
    }
    assert out["refused"] == "/notes/5: there's no item 5"
    assert out["rebased"] is True and out["rebase_post"] == 4
    assert out["after_rebase"] == '["a","theirs","mine"]'
    assert out["dropped"] is False and out["version"] == 6
    assert out["after_drop"] == '{"notes":[]}'
    assert out["by_place"] is False and out["after_by_place"] == '["new","b"]'
    # Guarded, but the test fails on the new list (item 1 is "new" now): dropped too.
    assert out["guarded"] is False
    assert out["after_guarded"] == '["newer","new","b"]'
