const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

const browse = require("../../browse/handler").runtime;
const act = require("../../browser-act/handler").runtime;
const read = require("../../browser-read/handler").runtime;
const handoff = require("../../browser-handoff/handler").runtime;

// A fake browser-runner: answers each request with respond(op, args).
async function fakeRunner(respond) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "bw-sock-"));
  const socket = path.join(dir, "runner.sock");
  const requests = [];
  const server = net.createServer((conn) => {
    let buffer = "";
    conn.on("data", (chunk) => {
      buffer += chunk;
      if (!buffer.includes("\n")) return;
      const msg = JSON.parse(buffer.split("\n")[0]);
      requests.push(msg);
      conn.end(JSON.stringify(respond(msg.op, msg.args)) + "\n");
    });
  });
  await new Promise((r) => server.listen(socket, r));
  process.env.BROWSER_SOCKET = socket;
  return { requests, close: () => new Promise((r) => server.close(r)) };
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
      ["act", { scope: { workspace: "career", thread: "12" }, action: "click", ref: "e3", text: "" }],
      ["read", { scope: { workspace: "career", thread: "12" }, find: "next" }],
      ["read", { scope: { workspace: "_jobs", thread: "default" }, find: "" }],
    ]);
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
