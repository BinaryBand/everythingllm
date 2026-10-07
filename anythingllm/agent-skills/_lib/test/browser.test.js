const test = require("node:test");
const assert = require("node:assert/strict");
const { fakeService } = require("./fakeservice");

const browse = require("../../browse/handler").runtime;
const act = require("../../browser-act/handler").runtime;
const read = require("../../browser-read/handler").runtime;
const handoff = require("../../browser-handoff/handler").runtime;

// A fake browser-runner: answers each request with respond(op, args).
async function fakeRunner(respond) {
  const service = await fakeService(respond);
  process.env.BROWSER_SOCKET = service.socket;
  return service;
}

function agent(invocation = { workspace: { slug: "career" }, thread_id: 12 }) {
  return { logger: () => {}, introspect: () => {}, super: { handlerProps: { invocation } } };
}

const CARD = "[![Browser: x](https://h:8445/_live/browser/bw-0123456789abcdef.jpg)](https://h:8445/_live/browser/bw-0123456789abcdef)";

test("the browser skills send the call's own scope and give the card only when it's new", async () => {
  let fresh = true;
  const runner = await fakeRunner((op, args) => {
    if (op === "open") {
      const result = { ok: true, result: { page: "Page: x", card: CARD, new: fresh } };
      fresh = false;
      return result;
    }
    return { ok: true, result: { page: `${op} page` } };
  });
  try {
    const first = await browse.handler.call(agent(), { url: "example.com", workspace: "education" });
    assert.match(first, /^Card: \[!\[Browser: x\]/);
    assert.match(first, /Put the Card line in your reply exactly as given/);
    assert.ok(first.endsWith("Page: x"));
    assert.equal(await browse.handler.call(agent(), { url: "example.com" }), "Page: x");
    assert.equal(await act.handler.call(agent(), { action: "click", ref: " e3 " }), "act page");
    assert.equal(await read.handler.call(agent(), { find: "next" }), "read page");
    // Scheduled jobs have no workspace or thread.
    await read.handler.call(agent({}), {});
    assert.deepEqual(runner.requests.map((r) => [r.op, r.args]), [
      ["open", { scope: { workspace: "career", thread: "12" }, url: "example.com" }],
      ["open", { scope: { workspace: "career", thread: "12" }, url: "example.com" }],
      ["label", { scope: { workspace: "career", thread: "12" }, ref: "e3" }],
      ["act", { scope: { workspace: "career", thread: "12" }, action: "click", ref: "e3", text: "" }],
      ["read", { scope: { workspace: "career", thread: "12" }, find: "next" }],
      ["read", { scope: { workspace: "_jobs", thread: "default" }, find: "" }],
    ]);
  } finally {
    delete process.env.BROWSER_SOCKET;
    await runner.close();
  }
});

test("each browser step says in the chat what it does, by the element's name and never what's typed", async () => {
  const runner = await fakeRunner((op, args) => {
    if (op === "label") return args.ref === "e4" ? { ok: false, error: "unknown op 'label'" } : { ok: true, result: { label: args.ref === "e2" ? "Customer name" : "" } };
    if (op === "open") return { ok: true, result: { page: "Page: x", card: CARD, new: false } };
    if (op === "handoff") return { ok: true, result: args.done ? { page: "Page: x" } : { card: CARD, takeover: "t" } };
    return { ok: true, result: { page: "Page: x" } };
  });
  const lines = [];
  const self = { ...agent(), introspect: (m) => lines.push(m) };
  try {
    await browse.handler.call(self, { url: "https://httpbin.org/forms/post?token=s3cret" });
    await browse.handler.call(self, { url: "example.com/x" });
    await act.handler.call(self, { action: "fill", ref: "e2", text: "hunter2" });
    await act.handler.call(self, { action: "click", ref: "e3" }); // no name: its ref
    await act.handler.call(self, { action: "click", ref: "e4" }); // a runner without label
    await act.handler.call(self, { action: "select", ref: "e2", text: "Large" });
    await act.handler.call(self, { action: "press", text: "Enter" });
    await act.handler.call(self, { action: "press", text: "h" });
    await act.handler.call(self, { action: "scroll_down" });
    await read.handler.call(self, {});
    await read.handler.call(self, { find: "total" });
    await handoff.handler.call(self, { reason: "log in" });
    await handoff.handler.call(self, { done: true });
    assert.deepEqual(lines, [
      "Opening httpbin.org",
      "Opening example.com",
      "Filling in Customer name",
      "Clicking e3",
      "Clicking e4",
      "Choosing an option in Customer name",
      "Pressing Enter",
      "Pressing a key",
      "Scrolling down",
      "Reading the page",
      "Looking for something on the page",
      "Handing the browser to you",
      "Taking the browser back",
    ]);
    assert.ok(!lines.join("\n").match(/hunter2|Large|s3cret|\bh\b/));
    assert.equal(runner.requests.filter((r) => r.op === "act").length, 7); // a failed label stops none
  } finally {
    delete process.env.BROWSER_SOCKET;
    await runner.close();
  }
});

test("handing the browser over tells the agent to end its reply, and done takes it back", async () => {
  const runner = await fakeRunner((op, args) =>
    args.done ? { ok: true, result: { page: "Page: logged in" } } : { ok: true, result: { card: CARD, takeover: "https://h:8454/t/" } }
  );
  try {
    const over = await handoff.handler.call(agent(), { reason: "log in to LinkedIn" });
    assert.match(over, /^Card: /);
    assert.match(over, /\(log in to LinkedIn\)/);
    assert.match(over, /Then end your reply/);
    assert.equal(await handoff.handler.call(agent(), { done: "true" }), "You have the browser again.\nPage: logged in");
    assert.deepEqual(runner.requests.map((r) => r.args), [
      { scope: { workspace: "career", thread: "12" }, reason: "log in to LinkedIn" },
      { scope: { workspace: "career", thread: "12" }, done: true },
    ]);
  } finally {
    delete process.env.BROWSER_SOCKET;
    await runner.close();
  }
});

test("a refusal and a missing runner become replies", async () => {
  const runner = await fakeRunner(() => ({ ok: false, error: "the user has this workspace's browser (log in)." }));
  try {
    assert.equal(await act.handler.call(agent(), { action: "click", ref: "e1" }), "Error: the user has this workspace's browser (log in).");
  } finally {
    await runner.close();
  }
  process.env.BROWSER_SOCKET = "/nonexistent/browser.sock";
  try {
    assert.match(await browse.handler.call(agent(), { url: "x" }), /browser service isn't running.*uv run hostctl browser-setup/);
  } finally {
    delete process.env.BROWSER_SOCKET;
  }
});

const login = require("../../browser-login/handler").runtime;

test("browser-login lists logins without secrets, fills one, and waits for the user's OK", async () => {
  let asked = 0;
  const runner = await fakeRunner((op, args) => {
    if (op === "logins")
      return { ok: true, result: { site: "www.linkedin.com", logins: [{ id: "3f2a9c1d", site: "linkedin.com", username: "alice", totp: true, ask: true, here: true }] } };
    if (op === "login" && asked++ === 0) return { ok: true, result: { approval: "ap1", card: CARD } };
    if (op === "wait_approval") return { ok: true, result: { done: true, approved: true } };
    return { ok: true, result: { page: "Page: feed" } };
  });
  try {
    const listed = await login.handler.call(agent(), { action: "list" });
    assert.match(listed, /this chat's page is on www\.linkedin\.com/);
    assert.match(listed, /- 3f2a9c1d: linkedin\.com as alice, with 2FA codes, asks the user first \(fits this page\)/);
    const lines = [];
    const self = { ...agent(), introspect: (m) => lines.push(m) };
    const done = await login.handler.call(self, { action: "login", login: "3f2a9c1d", user_ref: "e1", pass_ref: "e2", submit: "true" });
    assert.equal(done, "Page: feed");
    assert.match(lines[0], /Waiting for your OK in the browser/);
    assert.deepEqual(runner.requests.map((r) => r.op), ["logins", "login", "wait_approval", "login"]);
    assert.deepEqual(runner.requests[1].args, {
      scope: { workspace: "career", thread: "12" }, login: "3f2a9c1d", user_ref: "e1", pass_ref: "e2", submit: true,
    });
    assert.match(await login.handler.call(agent(), { action: "steal" }), /^Error: action is list, ask, login, code or passkey/);
  } finally {
    delete process.env.BROWSER_SOCKET;
    await runner.close();
  }
});

test("browser-login lists a passkey and signs in with it through the user's OK", async () => {
  let asked = 0;
  const runner = await fakeRunner((op) => {
    if (op === "logins")
      return { ok: true, result: { site: "github.com", logins: [{ id: "7c01e2aa", kind: "passkey", site: "github.com", username: "alice", totp: false, ask: true, here: true }] } };
    if (op === "passkey" && asked++ === 0) return { ok: true, result: { approval: "ap3", card: CARD } };
    if (op === "wait_approval") return { ok: true, result: { done: true, approved: true } };
    return { ok: true, result: { page: "Page: dashboard" } };
  });
  try {
    assert.match(await login.handler.call(agent(), { action: "list" }), /- 7c01e2aa: a passkey for github\.com as alice, asks the user first \(fits this page\)/);
    const self = { ...agent(), introspect: () => {} };
    assert.equal(await login.handler.call(self, { action: "passkey", login: "7c01e2aa", ref: "e7", submit: true }), "Page: dashboard");
    assert.deepEqual(runner.requests.map((r) => r.op), ["logins", "passkey", "wait_approval", "passkey"]);
    assert.deepEqual(runner.requests[1].args, { scope: { workspace: "career", thread: "12" }, login: "7c01e2aa", ref: "e7" });
  } finally {
    delete process.env.BROWSER_SOCKET;
    await runner.close();
  }
});

test("browser-login says so when the user refuses", async () => {
  const runner = await fakeRunner((op) =>
    op === "code" ? { ok: true, result: { approval: "ap2", card: CARD } } : { ok: true, result: { done: true, approved: false } }
  );
  try {
    assert.match(await login.handler.call(agent(), { action: "code", login: "x", ref: "e3" }), /didn't allow that login/);
  } finally {
    delete process.env.BROWSER_SOCKET;
    await runner.close();
  }
});

test("browser-login asks the user for a login on a card, with the scope and nothing from the model", async () => {
  const ASKED = "[![Log in to github.com](https://h:8445/_live/browser/login/lr-0.png)](https://h:8445/_live/browser/login/lr-0)";
  let card = ASKED;
  const runner = await fakeRunner(() => ({ ok: true, result: { request: "lr-0", site: "github.com", card } }));
  try {
    const reply = await login.handler.call(agent(), { action: "ask", login: "evil.example" });
    assert.match(reply, /^Card: \[!\[Log in to github\.com\]/);
    assert.match(reply, /save their github\.com login there \(never in the chat\), and end your reply/);
    assert.deepEqual(runner.requests.map((r) => [r.op, r.args]), [["ask_login", { scope: { workspace: "career", thread: "12" } }]]);
    card = ""; // no public host: no card
    assert.match(await login.handler.call(agent(), { action: "ask" }), /^Ask the user to save their github\.com login in the browser's take-over view/);
  } finally {
    delete process.env.BROWSER_SOCKET;
    await runner.close();
  }
});
