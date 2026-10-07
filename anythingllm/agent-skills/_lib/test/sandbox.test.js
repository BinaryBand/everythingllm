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
