// app.js: an app page's way to its data (packages/sandbox, sandbox.apps), which the host
// puts beside every app's page. The page loads it and starts it with what the host
// embedded:
//
//   <script src="app.js"></script>
//   <script>const data = App.start(/*APP*/null); App.onchange(draw); draw();</script>
//
//   App.start(app)       takes what the host embedded; returns the data (App.data), the one
//                        object for good: every change is made in it
//   App.data, App.title, App.card, App.version   the app as this page has it now
//   App.patch(ops)       change it: a JSON patch (RFC 6902), applied here at once and saved
//                        shortly after, a few at a time; a promise of whether it was saved.
//                        Paths are in the data (/items/0), but /title and /card
//   App.set(path, v)     replace what's at path, or add it
//   App.get(path)        what's at path
//   App.onchange(fn)     fn(App.data) after every change: this page's own patch, and one
//                        from elsewhere (another tab, the agent, a run)
//   App.onstatus(fn)     fn(state, text): saving, saved, offline, conflict, reload, error
//
// Forms don't submit on the pages site (its CSP sandbox has no allow-forms), so a form's
// submit event never fires: this turns Enter in one of its fields and a click on its
// submit button into a submit event its handler sees. Nothing is sent by it.
//
// A change made here while another was saved elsewhere is applied again on top of it; one
// that no longer applies (a `test` that fails, an item that's gone) is dropped, and the
// page shows the app as it is now; so is one that changes a list's item by its place (remove
// /items/3) without a test of that item, which could now be another. Guard them:
//   [{op: "test", path: "/items/3/text", value: "Eggs"},
//    {op: "replace", path: "/items/3/done", value: true}]
"use strict";
const App = (() => {
  const OWNED = ["title", "card", "data"];
  const SAVE_AFTER = 300; // ms after the last change
  const RETRY = 3000;
  const MAX_BODY = 256 * 1024;
  let app = null; // what the host embedded, then kept up to date
  let seen = 0; // the newest version sync has heard of, what it waits to move on from
  let polling = null; // the open sync's AbortController
  let queue = []; // changes not sent yet
  let flight = null; // changes being sent
  let waiting = []; // the promises of the changes in the queue: [resolve]
  let landing = []; // and of those in flight
  let timer = 0;
  let stopped = false;
  const onchange = [];
  const onstatus = [];

  const clone = (v) => (v === undefined ? v : JSON.parse(JSON.stringify(v)));
  const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);

  // --- JSON Patch, as the host applies it (sandbox/apps/patch.py) ---

  function pointer(path) {
    if (typeof path !== "string" || !path.startsWith("/")) throw new Error(`bad path ${path}`);
    const parts = path.slice(1).split("/").map((t) => t.replace(/~1/g, "/").replace(/~0/g, "~"));
    return OWNED.includes(parts[0]) ? parts : ["data", ...parts]; // the data's, but /title and /card
  }
  function index(list, token, path, appending) {
    if (appending && token === "-") return list.length;
    if (!/^(0|[1-9][0-9]*)$/.test(token)) throw new Error(`${path}: ${token} isn't an index`);
    const i = Number(token);
    if (i > list.length || (i === list.length && !appending)) throw new Error(`${path}: there's no item ${i}`);
    return i;
  }
  function parent(doc, parts, path) {
    let here = doc;
    for (const token of parts.slice(0, -1)) {
      if (Array.isArray(here)) here = here[index(here, token, path, false)];
      else if (here && typeof here === "object" && Object.hasOwn(here, token)) here = here[token];
      else throw new Error(`${path}: there's nothing at ${token}`);
    }
    return here;
  }
  function at(doc, path) {
    const parts = pointer(path);
    const where = parent(doc, parts, path);
    const last = parts[parts.length - 1];
    if (Array.isArray(where)) return where[index(where, last, path, false)];
    if (where && typeof where === "object" && Object.hasOwn(where, last)) return where[last];
    throw new Error(`${path}: there's nothing there`);
  }
  function remove(doc, path) {
    const parts = pointer(path);
    if (parts.length === 1) throw new Error(`${path} can't be removed`);
    const where = parent(doc, parts, path);
    const last = parts[parts.length - 1];
    if (Array.isArray(where)) return where.splice(index(where, last, path, false), 1)[0];
    if (where && typeof where === "object" && Object.hasOwn(where, last)) {
      const value = where[last];
      delete where[last];
      return value;
    }
    throw new Error(`${path}: there's nothing there`);
  }
  function add(doc, path, value) {
    const parts = pointer(path);
    const where = parent(doc, parts, path);
    const last = parts[parts.length - 1];
    if (Array.isArray(where)) where.splice(index(where, last, path, true), 0, value);
    else if (where && typeof where === "object") where[last] = value;
    else throw new Error(`${path}: its parent isn't an object or a list`);
  }
  function loose(ops) {
    // Whether a change touches a list's item by its place with no test of it first
    // (patch.py's unguarded): after a change elsewhere it could hit the wrong item, so it
    // isn't made again.
    const tested = [];
    const prefix = (a, b) => a.length <= b.length && a.every((t, i) => t === b[i]);
    for (const step of ops) {
      if (!["test", "remove", "replace", "move", "copy"].includes(step.op)) continue; // adding
      let parts;
      try {
        parts = pointer(step.op === "move" || step.op === "copy" ? step.from : step.path);
      } catch {
        continue;
      }
      if (step.op === "test") {
        tested.push(parts);
        continue;
      }
      const places = parts.flatMap((t, n) => (/^[0-9]+$/.test(t) ? [n] : []));
      if (!places.length) continue;
      const item = parts.slice(0, places[places.length - 1] + 1);
      if (!tested.some((t) => prefix(item, t) || prefix(t, item))) return true;
    }
    return false;
  }
  function apply(doc, ops) {
    doc = clone(doc);
    for (const step of ops) {
      const { op, path } = step;
      if (op === "add") add(doc, path, clone(step.value));
      else if (op === "remove") remove(doc, path);
      else if (op === "replace") {
        at(doc, path);
        const parts = pointer(path);
        if (parts.length === 1) doc[parts[0]] = clone(step.value);
        else {
          remove(doc, path);
          add(doc, path, clone(step.value));
        }
      } else if (op === "test") {
        if (!equal(at(doc, path), step.value)) throw new Error(`${path} isn't what the patch expected`);
      } else if (op === "move" || op === "copy") {
        const origin = pointer(step.from);
        const target = pointer(path);
        if (op === "move" && origin.every((t, i) => t === target[i])) throw new Error(`${step.from} can't move into itself`);
        add(doc, path, op === "move" ? remove(doc, step.from) : clone(at(doc, step.from)));
      } else throw new Error(`op must be one of: add, remove, replace, move, copy, test`);
    }
    return doc;
  }

  // --- state ---

  function doc() {
    return { title: app.title, card: app.card, data: app.data };
  }
  function adopt(next) {
    // In place, so a page that kept App.data (or what App.start returned) sees the change.
    app.title = next.title;
    app.card = next.card;
    app.data = into(app.data, next.data);
  }
  function into(kept, next) {
    const object = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
    if (Array.isArray(kept) && Array.isArray(next)) {
      kept.splice(0, kept.length, ...next);
      return kept;
    }
    if (object(kept) && object(next)) {
      for (const key of Object.keys(kept)) if (!Object.hasOwn(next, key)) delete kept[key];
      for (const key of Object.keys(next)) kept[key] = Object.hasOwn(kept, key) ? into(kept[key], next[key]) : next[key];
      return kept;
    }
    return next;
  }
  function status(state, text) {
    for (const fn of onstatus) fn(state, text || "");
  }
  function changed() {
    for (const fn of onchange) fn(app.data);
  }
  function settle(promises, saved) {
    for (const resolve of promises) resolve(saved);
  }

  // --- saving ---

  function later() {
    clearTimeout(timer);
    timer = setTimeout(flush, SAVE_AFTER);
  }

  async function post(url, body, signal) {
    const answer = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "text/plain" },
      body: JSON.stringify({ token: app.token, ...body }),
      signal,
    });
    return [answer.status, await answer.json().catch(() => ({}))];
  }

  async function flush() {
    if (stopped || flight || !queue.length) return;
    flight = queue;
    queue = [];
    landing = waiting;
    waiting = [];
    const body = { patch: flight, version: app.version };
    if (JSON.stringify(body).length > MAX_BODY - 1024) {
      status("error", "That change is too big to save at once.");
      return restart();
    }
    status("saving", "Saving…");
    let code, reply;
    try {
      [code, reply] = await post(app.ops, body);
    } catch {
      requeue();
      status("offline", "Couldn't reach the server; trying again.");
      setTimeout(flush, RETRY);
      return;
    }
    if (code === 200) {
      seen = app.version = reply.version;
      polling?.abort(); // it would only bring back this page's own change
      flight = null;
      settle(landing, true);
      landing = [];
      status("saved", "Saved");
      if (queue.length) later();
      return;
    }
    if (reply.reload) return stop();
    if (reply.conflict) return rebase(reply);
    if (code === 409 || code === 429) {
      requeue();
      status("offline", reply.error || "Busy; trying again.");
      setTimeout(flush, RETRY);
      return;
    }
    status("error", reply.error || `Couldn't save (${code}).`);
    return restart();
  }

  function requeue() {
    queue = flight.concat(queue);
    waiting = landing.concat(waiting);
    flight = null;
    landing = [];
  }

  function rebase(now) {
    // The app changed elsewhere: this page's changes again, on top of it, if they apply.
    const mine = flight.concat(queue);
    const promises = landing.concat(waiting);
    flight = null;
    landing = [];
    seen = app.version = now.version;
    try {
      if (loose(mine)) throw new Error("by place, untested");
      adopt(apply(now, mine));
      queue = mine;
      waiting = promises;
      status("conflict", "Changed elsewhere too; saving yours on top.");
      changed();
      later();
    } catch {
      adopt(now);
      queue = [];
      waiting = [];
      settle(promises, false);
      status("conflict", "Changed elsewhere; showing the latest.");
      changed();
    }
  }

  function drop() {
    // Every unsaved change goes, and its promise says so.
    flight = null;
    queue = [];
    settle(landing.concat(waiting), false);
    landing = [];
    waiting = [];
  }

  function stop() {
    stopped = true;
    drop();
    status("reload", "This page is out of date. Reload it.");
  }

  async function restart() {
    // A change the host refused: drop what's unsaved and show the app as it is.
    drop();
    try {
      const [code, reply] = await post(app.sync, { since: -1 });
      if (code === 200 && reply.data !== undefined) {
        adopt(reply);
        seen = app.version = reply.version;
        changed();
      }
    } catch {}
  }

  // --- keeping up with changes made elsewhere ---

  async function sync() {
    while (!stopped) {
      if (document.visibilityState === "hidden") {
        await new Promise((go) => document.addEventListener("visibilitychange", go, { once: true }));
        continue;
      }
      let code, reply;
      polling = new AbortController();
      try {
        [code, reply] = await post(app.sync, { since: seen }, polling.signal);
      } catch (e) {
        if (e.name !== "AbortError") await new Promise((go) => setTimeout(go, RETRY));
        continue;
      }
      if (reply.reload) return stop();
      if (code !== 200) {
        await new Promise((go) => setTimeout(go, RETRY * 2));
        continue;
      }
      seen = reply.version; // so the next poll waits for a newer one
      if (reply.data === undefined || reply.version === app.version) continue;
      if (flight || queue.length) continue; // saving will find out, and rebase
      adopt(reply);
      app.version = reply.version;
      changed();
    }
  }

  // --- forms, which the sandbox won't submit ---

  function submit(form, submitter) {
    // Checked first, as a real submit is: required, pattern and the like.
    if (!form.noValidate && !submitter?.formNoValidate && !form.checkValidity()) return form.reportValidity();
    const event = typeof SubmitEvent === "function"
      ? new SubmitEvent("submit", { bubbles: true, cancelable: true, submitter })
      : new Event("submit", { bubbles: true, cancelable: true });
    form.dispatchEvent(event);
  }
  const NOT_TEXT = ["button", "checkbox", "color", "file", "hidden", "image", "radio", "range", "reset", "submit"];
  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("click", (e) => {
      const button = e.target?.closest?.("button, input[type=submit], input[type=image]");
      if (!button?.form || (button.type || "").toLowerCase() !== "submit") return;
      e.preventDefault();
      submit(button.form, button);
    }, true);
    document.addEventListener("keydown", (e) => {
      const field = e.target;
      if (e.key !== "Enter" || e.isComposing || field?.tagName !== "INPUT" || !field.form) return;
      if (NOT_TEXT.includes((field.type || "").toLowerCase())) return;
      e.preventDefault();
      submit(field.form, null);
    }, true);
  }

  return {
    start(embedded) {
      if (!embedded || typeof embedded !== "object") throw new Error("App.start needs the app the host embedded");
      app = embedded;
      seen = app.version;
      sync();
      return app.data;
    },
    get data() {
      return app.data;
    },
    get title() {
      return app.title;
    },
    get card() {
      return app.card;
    },
    get version() {
      return app.version;
    },
    get(path) {
      return at(doc(), path);
    },
    patch(ops) {
      if (stopped) throw new Error("this page is out of date; reload it");
      if (!Array.isArray(ops) || !ops.length) throw new Error("a patch is a list of operations");
      adopt(apply(doc(), ops)); // throws, changing nothing, when it doesn't apply
      queue.push(...clone(ops));
      later();
      changed();
      return new Promise((resolve) => waiting.push(resolve));
    },
    set(path, value) {
      let there = true;
      try {
        at(doc(), path);
      } catch {
        there = false;
      }
      return this.patch([{ op: there ? "replace" : "add", path, value }]);
    },
    onchange(fn) {
      onchange.push(fn);
    },
    onstatus(fn) {
      onstatus.push(fn);
    },
  };
})();
