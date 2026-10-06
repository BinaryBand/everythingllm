const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

const { runtime } = require("../handler");

// A fake research-runner on a socket of its own: answers each request with respond(op, args).
async function fakeRunner(respond) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "dr-sock-"));
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
      if (reply !== undefined) conn.end(JSON.stringify(reply) + "\n");
    });
  });
  await new Promise((r) => server.listen(socket, r));
  process.env.RESEARCH_SOCKET = socket;
  return { requests, close: () => new Promise((r) => server.close(r)) };
}

function agent({ workspace = { slug: "career", name: "Career" }, runtimeArgs = {}, signal = null } = {}) {
  const lines = [];
  const citations = [];
  return {
    lines,
    citations,
    self: {
      runtimeArgs,
      introspect: (m) => lines.push(m),
      logger: () => {},
      super: {
        abortController: signal ? { signal } : undefined,
        addCitation: (c) => citations.push(...c),
        handlerProps: { invocation: { workspace } },
      },
    },
  };
}

test("a run's progress reaches the chat and its reply becomes the skill's", async () => {
  let waits = 0;
  const runner = await fakeRunner((op, args) => {
    if (op === "start") return { ok: true, result: { run_id: "dr-1", queued: 0 } };
    waits++;
    if (waits === 1) return { ok: true, result: { events: ["Planning quick research", "Plan: 1) A"], done: false, result: null } };
    return {
      ok: true,
      result: {
        events: ["published https://h/r/"],
        done: true,
        result: { status: "ok", reply: "Research report published: \"T\"", sources: [{ url: "https://a/", title: "A" }] },
      },
    };
  });
  try {
    const { self, lines, citations } = agent({ runtimeArgs: { PLANNER_MODEL: "glm-5.3", EMBED_IN_WORKSPACE: "no" } });
    const reply = await runtime.handler.call(self, { question: "Bitcoin?", depth: "quick" });
    assert.equal(reply, 'Research report published: "T"');
    assert.deepEqual(lines, [
      "Deep research: Planning quick research",
      "Deep research: Plan: 1) A",
      "Deep research: published https://h/r/",
    ]);
    assert.deepEqual(citations, [{ id: "https://a/", title: "A", text: "", chunkSource: "link://https://a/", score: null }]);
    const [start, first, second] = runner.requests;
    assert.deepEqual(start.args, {
      question: "Bitcoin?", depth: "quick", planner: "glm-5.3", worker: null, planner_fallback: null, site: null,
      embed: false, workspace: "career", workspace_name: "Career",
    });
    assert.deepEqual([first.args, second.args], [{ run_id: "dr-1", since: 0 }, { run_id: "dr-1", since: 2 }]);
  } finally {
    await runner.close();
  }
});

test("a closed chat stops waiting at once, and the run carries on", async () => {
  const ac = new AbortController();
  const runner = await fakeRunner((op) => {
    if (op === "start") return { ok: true, result: { run_id: "dr-2", queued: 0 } };
    setTimeout(() => ac.abort(), 20);
    return undefined; // a long poll that never answers
  });
  try {
    const { self } = agent({ signal: ac.signal });
    const t = Date.now();
    const reply = await runtime.handler.call(self, { question: "q" });
    assert.match(reply, /The chat closed; the research carries on/);
    assert.ok(Date.now() - t < 2000);
  } finally {
    await runner.close();
  }
});

test("a runner that isn't running gets a clear answer", async () => {
  process.env.RESEARCH_SOCKET = path.join(os.tmpdir(), `dr-none-${process.pid}.sock`);
  const { self } = agent();
  const reply = await runtime.handler.call(self, { question: "q" });
  assert.match(reply, /deep research service isn't running on the server \(ENOENT/);
  assert.match(reply, /make research-setup/);
});

test("a run the runner no longer knows ends the wait with what to tell the user", async () => {
  const runner = await fakeRunner((op) =>
    op === "start"
      ? { ok: true, result: { run_id: "dr-3", queued: 0 } }
      : { ok: false, error: "no research run 'dr-3' here (finished over an hour ago, or the runner restarted)." }
  );
  try {
    const reply = await runtime.handler.call(agent().self, { question: "q" });
    assert.match(reply, /^Lost touch with the deep research run \(no research run 'dr-3'/);
    assert.match(reply, /research site/);
    assert.equal(runner.requests.length, 2, "an unknown run isn't asked about again");
  } finally {
    await runner.close();
  }
});

test("a start the runner refuses says why", async () => {
  const runner = await fakeRunner(() => ({ ok: false, error: "No research question was given." }));
  try {
    const reply = await runtime.handler.call(agent().self, { question: " " });
    assert.match(reply, /couldn't start: No research question was given\./);
  } finally {
    await runner.close();
  }
});
