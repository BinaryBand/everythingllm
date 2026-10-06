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

function agent({ workspace = { slug: "career", name: "Career" }, runtimeArgs = {} } = {}) {
  return {
    runtimeArgs,
    introspect: () => {},
    logger: () => {},
    super: { handlerProps: { invocation: { workspace } } },
  };
}

const CARD = "[![Deep research: Bitcoin?](https://h:8445/_live/research/dr-1.png)](https://h:8445/_live/research/dr-1)";

test("a run is started and the skill answers at once with its live card", async () => {
  const runner = await fakeRunner(() => ({ ok: true, result: { run_id: "dr-1", queued: 0, card: CARD } }));
  try {
    const self = agent({ runtimeArgs: { PLANNER_MODEL: "glm-5.3" } });
    const reply = await runtime.handler.call(self, {
      question: "Bitcoin?",
      depth: "quick",
      sub_questions: '["Price history", {"goal": "Energy use"}]',
      title: "Bitcoin",
    });
    assert.match(reply, /^Deep research started \(run dr-1\)\. .*adding it to this workspace's documents/);
    assert.ok(reply.includes(`\n\nCard: ${CARD}\n\n`));
    assert.match(reply, /Put the Card line in your reply exactly as given/);
    assert.doesNotMatch(reply, /waits for/);
    assert.deepEqual(runner.requests, [
      {
        op: "start",
        args: {
          question: "Bitcoin?", depth: "quick", planner: "glm-5.3", worker: null, planner_fallback: null, site: null,
          embed: true, workspace: "career", workspace_name: "Career",
          sub_questions: ["Price history", { goal: "Energy use" }], title: "Bitcoin",
        },
      },
    ]);
  } finally {
    await runner.close();
  }
});

test("a queued run says so, and without a card or embedding the reply says less", async () => {
  const runner = await fakeRunner(() => ({ ok: true, result: { run_id: "dr-2", queued: 2, card: "" } }));
  try {
    const reply = await runtime.handler.call(agent({ runtimeArgs: { EMBED_IN_WORKSPACE: "no" } }), { question: "q" });
    assert.match(reply, /It waits for 2 other research runs to finish first\./);
    assert.doesNotMatch(reply, /Card:|workspace's documents/);
    assert.match(reply, /the report will be on the research site/);
    assert.equal(runner.requests[0].args.embed, false);
    assert.equal(runner.requests[0].args.sub_questions, null);
    assert.equal(runner.requests[0].args.title, null);
  } finally {
    await runner.close();
  }
});

test("a runner that isn't running gets a clear answer", async () => {
  process.env.RESEARCH_SOCKET = path.join(os.tmpdir(), `dr-none-${process.pid}.sock`);
  const reply = await runtime.handler.call(agent(), { question: "q" });
  assert.match(reply, /deep research service isn't running on the server \(ENOENT/);
  assert.match(reply, /uv run hostctl research-setup/);
});

test("a start the runner refuses says why", async () => {
  const runner = await fakeRunner(() => ({ ok: false, error: "No research question was given." }));
  try {
    const reply = await runtime.handler.call(agent(), { question: " " });
    assert.match(reply, /couldn't start: No research question was given\./);
  } finally {
    await runner.close();
  }
});
