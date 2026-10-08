const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

const runCode = require("../../run-code/handler").runtime;
const writeFile = require("../../write-file/handler").runtime;
const publish = require("../../publish/handler").runtime;
const buildSite = require("../../build-site/handler").runtime;
const showImage = require("../../show-image/handler").runtime;
const sandboxAccess = require("../../sandbox-access/handler").runtime;
const app = require("../../app/handler").runtime;

// A fake sandbox-runner: answers each request with respond(op, args); an array of Buffers
// is sent as separate chunks, a moment apart, and undefined means never.
async function fakeRunner(respond) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "sbx-sock-"));
  const socket = path.join(dir, "runner.sock");
  const requests = [];
  const server = net.createServer((conn) => {
    let buffer = "";
    conn.on("data", async (chunk) => {
      buffer += chunk;
      if (!buffer.includes("\n")) return;
      const msg = JSON.parse(buffer.split("\n")[0]);
      requests.push(msg);
      const reply = await respond(msg.op, msg.args);
      if (reply === undefined) return;
      if (!Array.isArray(reply)) return conn.end(JSON.stringify(reply) + "\n");
      for (const chunk of reply) {
        conn.write(chunk);
        await new Promise((r) => setTimeout(r, 20));
      }
      conn.end();
    });
  });
  await new Promise((r) => server.listen(socket, r));
  process.env.SANDBOX_SOCKET = socket;
  return { requests, close: () => new Promise((r) => server.close(r)) };
}

function agent({ invocation = { workspace: { slug: "career" }, thread_id: 12 }, signal = null } = {}) {
  const lines = [];
  return {
    lines,
    self: {
      introspect: (m) => lines.push(m),
      logger: () => {},
      super: { abortController: signal ? { signal } : undefined, handlerProps: { invocation } },
    },
  };
}

const done = {
  exit_code: 0, timed_out: false, oom_killed: false, timeout: 60, seconds: 0.4,
  stdout: "hi\n", stderr: "", changed: ["/work/plot.png"], changed_more: 0,
  workspace_bytes: 10, warn_bytes: 100,
};

test("run-code sends its scope, waits out a long run and formats the result", async () => {
  const runner = await fakeRunner((op) =>
    op === "run" ? { ok: true, result: { running: true, run_id: "r-1", seconds: 45 } } : { ok: true, result: done }
  );
  try {
    const { self, lines } = agent();
    const reply = await runCode.handler.call(self, { language: "python", code: "print('hi')" });
    assert.equal(reply, "exit code: 0 (0.4s)\nstdout:\nhi\nfiles created or changed: /work/plot.png");
    assert.deepEqual(runner.requests, [
      { op: "run", args: { scope: { workspace: "career", thread: "12" }, language: "python", code: "print('hi')", timeout: 60 } },
      { op: "wait", args: { scope: { workspace: "career", thread: "12" }, run_id: "r-1" } },
    ]);
    assert.deepEqual(lines, ["Still running (45 s)…"]);
  } finally {
    await runner.close();
  }
});

test("a call with no thread or workspace gets the default scopes", async () => {
  const runner = await fakeRunner(() => ({ ok: true, result: { ...done, timed_out: true, stdout: "", warning: "it's big" } }));
  try {
    const reply = await runCode.handler.call(agent({ invocation: { workspace: null } }).self, { language: "bash", code: "x", timeout: 5 });
    assert.match(reply, /^TIMED OUT after 60s/);
    assert.match(reply, /\nwarning: it's big$/);
    assert.deepEqual(runner.requests[0].args.scope, { workspace: "_jobs", thread: "default" });
    assert.equal(runner.requests[0].args.timeout, 5);
  } finally {
    await runner.close();
  }
});

test("output split mid-character arrives whole", async () => {
  const bytes = Buffer.from(JSON.stringify({ ok: true, result: { ...done, stdout: "π ≈ 3.14 ✓\n" } }) + "\n");
  const cut = bytes.indexOf(Buffer.from("π")) + 1; // inside the two bytes of π
  const runner = await fakeRunner(() => [bytes.subarray(0, cut), bytes.subarray(cut)]);
  try {
    const reply = await runCode.handler.call(agent().self, { language: "python", code: "x" });
    assert.match(reply, /stdout:\nπ ≈ 3\.14 ✓/);
  } finally {
    await runner.close();
  }
});

test("runner errors become replies, never throws", async () => {
  const runner = await fakeRunner(() => ({ ok: false, error: "'x' is taken on the pages site; choose another slug" }));
  try {
    const reply = await publish.handler.call(agent().self, { path: "/work/x", slug: "x" });
    assert.equal(reply, "Error: 'x' is taken on the pages site; choose another slug");
  } finally {
    await runner.close();
  }
  process.env.SANDBOX_SOCKET = path.join(os.tmpdir(), "no-such-sandbox.sock");
  assert.match(await writeFile.handler.call(agent().self, { path: "a", content: "a" }), /isn't running.*uv run hostctl sandbox-setup/);
});

test("a closed chat stops waiting on a run", async () => {
  const runner = await fakeRunner((op) => (op === "run" ? { ok: true, result: { running: true, run_id: "r-1", seconds: 45 } } : undefined));
  try {
    const abort = new AbortController();
    const started = Date.now();
    setTimeout(() => abort.abort(), 50);
    const reply = await runCode.handler.call(agent({ signal: abort.signal }).self, { language: "python", code: "x" });
    assert.match(reply, /chat closed; the run carries on/);
    assert.ok(Date.now() - started < 2000);
  } finally {
    await runner.close();
  }
});

test("write-file and publish replies", async () => {
  const runner = await fakeRunner((op, args) => {
    if (op === "write") return { ok: true, result: args.delete ? { path: args.path, folder: true } : { path: args.path, bytes: 3 } };
    if (args.remove) return { ok: true, result: { slug: args.slug, removed: true } };
    return {
      ok: true,
      result: { slug: args.slug, url: "https://h/plot/", files: 2, blocked: ["scripts from another host"], notices: ["it has scripts"] },
    };
  });
  try {
    const { self } = agent();
    assert.equal(await writeFile.handler.call(self, { path: "/project/a.csv", content: "a,b" }), "wrote /project/a.csv (3 bytes)");
    assert.equal(await writeFile.handler.call(self, { path: "/work/out", delete: true }), "deleted folder /work/out");
    assert.equal(
      await publish.handler.call(self, { path: "/work/out", slug: "plot" }),
      "live: https://h/plot/ (2 files)\nwarning: the pages site blocks scripts from another host; it will show without them\nnote: it has scripts"
    );
    assert.equal(await publish.handler.call(self, { slug: "plot", remove: true }), "removed /public/plot; it's no longer on the web");
    assert.deepEqual(runner.requests[2].args, { scope: { workspace: "career", thread: "12" }, slug: "plot", path: "/work/out", remove: false });
  } finally {
    await runner.close();
  }
});

test("show-image gives the line that shows the image, with the invocation's scope", async () => {
  const image = "[![Totals](https://h:8445/_images/career/ab.png)](https://h:8445/_images/career/ab.png)";
  const runner = await fakeRunner(() => ({
    ok: true,
    result: { url: "https://h:8445/_images/career/ab.png", width: 1200, height: 800, bytes: 48_000, image },
  }));
  try {
    const reply = await showImage.handler.call(agent().self, { path: "/work/totals.png", alt: "Totals", scope: { workspace: "home" } });
    assert.equal(reply, `shown: https://h:8445/_images/career/ab.png (1200×800, 47 KB)\nImage: ${image}`);
    assert.deepEqual(runner.requests[0], {
      op: "show_image",
      args: { path: "/work/totals.png", alt: "Totals", scope: { workspace: "career", thread: "12" } },
    });
  } finally {
    await runner.close();
  }
});

test("the pages a call changed come back with run-code and write-file, and publish lists them", async () => {
  const published = {
    live: [
      { slug: "notes", url: "https://h/career/notes/", blocked: ["scripts from another host"], notices: ["it has scripts", "no new tabs"] },
    ],
    removed: ["old"],
  };
  let pages = [{ slug: "notes", url: "https://h/career/notes/" }];
  const runner = await fakeRunner((op) => {
    if (op === "run") return { ok: true, result: { ...done, changed: ["/public/notes/index.html"], published } };
    if (op === "write") return { ok: true, result: { path: "/public/old", folder: true, published: { removed: ["old"] } } };
    if (op === "publish") return { ok: true, result: { site: "https://h/career/", pages } };
  });
  try {
    const { self } = agent();
    const reply = await runCode.handler.call(self, { language: "bash", code: "x" });
    assert.match(
      reply,
      /live: https:\/\/h\/career\/notes\/\nwarning: the pages site blocks scripts from another host in notes; it will show without them\nnote \(notes\): it has scripts\nnote \(notes\): no new tabs\ngone: \/public\/old$/
    );
    assert.equal(
      await writeFile.handler.call(self, { path: "/public/old", delete: true }),
      "deleted folder /public/old\ngone: /public/old"
    );
    assert.equal(
      await publish.handler.call(self, {}),
      "This workspace's pages (https://h/career/):\n- notes: https://h/career/notes/"
    );
    pages = [];
    assert.match(await publish.handler.call(self, {}), /no pages yet; whatever goes in \/public is live at https:\/\/h\/career\//);
  } finally {
    await runner.close();
  }
});

test("build-site sends the path and slug, waits out a long build and says what went live", async () => {
  const runner = await fakeRunner((op) =>
    op === "build_site"
      ? { ok: true, result: { running: true, run_id: "r-9", seconds: 45 } }
      : { ok: true, result: { slug: "lab", url: "https://h/career/lab/", files: 8, zola: "Done", published: { live: [{ slug: "lab", url: "https://h/career/lab/", blocked: [] }] } } }
  );
  try {
    const { self, lines } = agent();
    assert.equal(
      await buildSite.handler.call(self, { path: "/shared/career/sites/lab", slug: "lab" }),
      "built 8 files into /public/lab\nlive: https://h/career/lab/"
    );
    assert.deepEqual(runner.requests[0].args, { scope: { workspace: "career", thread: "12" }, path: "/shared/career/sites/lab", slug: "lab" });
    assert.deepEqual(runner.requests[1], { op: "wait", args: { scope: { workspace: "career", thread: "12" }, run_id: "r-9" } });
    assert.match(lines[0], /Still building \(45 s\)/);
  } finally {
    await runner.close();
  }
});

// A UI chat (its invocation row's uuid), asking the user with `answer` when a skill wants approval.
function uiChat(answer) {
  const asked = [];
  return {
    asked,
    self: {
      introspect: () => {},
      logger: () => {},
      super: { handlerProps: { invocation: { uuid: "inv-1", workspace: { slug: "career" }, thread_id: 12 } } },
      requestToolApproval: async (request) => {
        asked.push(request);
        return answer;
      },
    },
  };
}

function accessRunner(web = false) {
  const now = { web, models: false, daily_tokens: 200000 };
  return fakeRunner((op, args) => {
    if (op !== "access") return { ok: false, error: "?" };
    const would = { ...now };
    for (const k of ["web", "models", "daily_tokens"]) if (args[k] !== undefined) would[k] = args[k];
    if (JSON.stringify(would) === JSON.stringify(now)) return { ok: true, result: { workspace: "career", ...now, changed: false } };
    const on = (would.web && !now.web) || (would.models && !now.models) || would.daily_tokens > now.daily_tokens;
    if (!args.apply) return { ok: true, result: { workspace: "career", ...now, would, needs_approval: on } };
    if (on && !args.approved) return { ok: false, error: "turning access on needs the user's approval" };
    return { ok: true, result: { workspace: "career", ...would, changed: true } };
  });
}

test("sandbox-access turns web on only with the user's own approval in a UI chat", async () => {
  const runner = await accessRunner();
  try {
    const { self, asked } = uiChat({ approved: true, message: "User approved the tool execution." });
    assert.match(await sandboxAccess.handler.call(self, {}), /web access is off/);
    assert.match(await sandboxAccess.handler.call(self, { web: "on" }), /apply true would make it: web access is on.*after the user approves it in the chat\./);
    assert.equal(asked.length, 0);
    assert.match(await sandboxAccess.handler.call(self, { web: "on", apply: true }), /^Done\. In this workspace, web access is on/);
    assert.equal(asked.length, 1);
    assert.match(asked[0].description, /reach public websites/);
    const last = runner.requests.at(-1);
    assert.deepEqual(last.args, { web: true, apply: true, approved: true, scope: { workspace: "career", thread: "12" } });
  } finally {
    await runner.close();
  }
});

test("sandbox-access never counts an approval AnythingLLM gave without asking", async () => {
  for (const message of [
    "Skill is whitelisted - auto-approved.",
    "Skill is auto-approved.",
    "Approval not required in this context.",
    "Auto-approved by scheduled job runner.",
  ]) {
    const runner = await accessRunner();
    try {
      const reply = await sandboxAccess.handler.call(uiChat({ approved: true, message }).self, { web: "on", apply: true });
      assert.match(reply, /Nothing changed: AnythingLLM approved it without asking/, message);
      assert.equal(runner.requests.filter((r) => r.args.apply).length, 0, message);
    } finally {
      await runner.close();
    }
  }
  const runner = await accessRunner();
  try {
    const reply = await sandboxAccess.handler.call(uiChat({ approved: false, message: "Tool call was rejected by the user." }).self, { web: "on", apply: true });
    assert.match(reply, /Nothing changed: Tool call was rejected by the user\./);
    // An API or Telegram chat (no invocation row) can't turn it on, and never asks.
    const api = uiChat({ approved: true, message: "User approved the tool execution." });
    delete api.self.super.handlerProps.invocation.uuid;
    assert.match(await sandboxAccess.handler.call(api.self, { web: "on", apply: true }), /can only be turned on or raised from a chat in AnythingLLM's own window/);
    assert.equal(api.asked.length, 0);
    assert.equal(runner.requests.filter((r) => r.args.apply).length, 0);
  } finally {
    await runner.close();
  }
});

test("sandbox-access turns web off from any chat, without asking", async () => {
  const runner = await accessRunner(true);
  try {
    const api = uiChat(null);
    delete api.self.super.handlerProps.invocation.uuid;
    assert.match(await sandboxAccess.handler.call(api.self, { web: "off", apply: "true" }), /^Done\. In this workspace, web access is off/);
    assert.equal(api.asked.length, 0);
    assert.match(await sandboxAccess.handler.call(api.self, { web: "maybe" }), /web and models must be "on" or "off"/);
  } finally {
    await runner.close();
  }
});

test("sandbox-access asks before model access or a bigger budget, and says what it is", async () => {
  const runner = await accessRunner();
  try {
    const { self, asked } = uiChat({ approved: true, message: "User approved the tool execution." });
    const reply = await sandboxAccess.handler.call(self, { models: "on", daily_tokens: "300000", apply: true });
    assert.match(reply, /model access is on \(runs can ask a model, up to 300000 tokens a day/);
    assert.match(asked[0].description, /ask a model on the server's account, up to 300000 tokens a day/);
    assert.doesNotMatch(asked[0].description, /public websites/);
    assert.deepEqual(runner.requests.at(-1).args, {
      models: true, daily_tokens: 300000, apply: true, approved: true, scope: { workspace: "career", thread: "12" },
    });
    assert.match(await sandboxAccess.handler.call(self, { daily_tokens: "lots" }), /daily_tokens must be a whole number/);
  } finally {
    await runner.close();
  }
});

test("app sends one op with the invocation's scope and replies with the card", async () => {
  const card = "[![Groceries](https://h:8445/_live/apps/career/groceries.png)](https://h:8445/_live/apps/career/groceries)";
  const runner = await fakeRunner((op, args) => {
    if (args.action === "list") return { ok: true, result: { apps: [{ name: "groceries", title: "Groceries", summary: "4 of 6 left" }, { name: "old", error: "its data can't be used" }] } };
    if (args.action === "delete") return { ok: true, result: { name: args.name, deleted: true } };
    return { ok: true, result: { name: "groceries", title: "Groceries", summary: "4 of 6 left", did: "added oat milk", card, page: "https://h:8447/career/apps/groceries/" } };
  });
  try {
    const { self } = agent();
    const reply = await app.handler.call(self, { action: "do", name: "groceries", op: "add", item: "Oat milk", scope: { workspace: "home" } });
    assert.equal(reply, `Added oat milk. Groceries: 4 of 6 left.\nCard: ${card}\nPage: https://h:8447/career/apps/groceries/`);
    assert.deepEqual(runner.requests[0], {
      op: "app",
      args: { action: "do", name: "groceries", title: "", op: "add", args: { item: "Oat milk" }, scope: { workspace: "career", thread: "12" } },
    });
    await app.handler.call(self, { action: "create", name: "packing", title: "Packing", items: '["Passport", "Charger"]' });
    assert.deepEqual(runner.requests[1].args.args, { items: ["Passport", "Charger"] });
    await app.handler.call(self, { action: "do", name: "packing", op: "rename", title: "Trip" });
    assert.deepEqual(runner.requests[2].args.args, { title: "Trip" });
    assert.match(await app.handler.call(self, {}), /^This workspace's apps:\n- groceries: Groceries, 4 of 6 left\n- old: its data can't be used$/);
    assert.match(await app.handler.call(self, { action: "delete", name: "groceries" }), /Deleted the app groceries/);
  } finally {
    await runner.close();
  }
});
