const test = require("node:test");
const assert = require("node:assert/strict");
const os = require("os");
const path = require("path");

const { runtime } = require("../handler");
const { fakeService } = require("../../_lib/test/fakeservice");

// Never the live agents-runner: a test that follows a run sets its own.
const NO_AGENTS = path.join(os.tmpdir(), `dr-no-agents-${process.pid}.sock`);
process.env.AGENTS_SOCKET = NO_AGENTS;

// A fake agents-runner, which follows runs for their chats.
async function fakeAgents() {
  const service = await fakeService((op) => ({ ok: true, result: { following: op } }));
  process.env.AGENTS_SOCKET = service.socket;
  return {
    requests: service.requests,
    close: () => {
      process.env.AGENTS_SOCKET = NO_AGENTS;
      return service.close();
    },
  };
}

// A fake research-runner: answers each request with respond(op, args).
async function fakeRunner(respond) {
  const service = await fakeService(respond);
  process.env.RESEARCH_SOCKET = service.socket;
  return service;
}

// A chat in AnythingLLM's UI has an invocation row: its uuid and thread_id (null in the main
// chat). An API or Telegram chat's (`chat: "api"`) has thread_id but no uuid
// (thread-scope.js), and a scheduled job's (`chat: false`) neither.
function agent({ workspace = { slug: "career", name: "Career" }, thread_id = null, chat = true, runtimeArgs = {} } = {}) {
  const invocation = chat === "api" ? { workspace, workspace_id: 3, thread_id } : chat ? { uuid: "inv-1", workspace, thread_id } : { workspace };
  return {
    runtimeArgs,
    introspect: () => {},
    logger: () => {},
    super: { handlerProps: { invocation } },
  };
}

const CARD = "[![Deep research: Bitcoin?](https://h:8445/_live/research/dr-1.png)](https://h:8445/_live/research/dr-1)";

test("a run is started and the skill answers at once with its live card", async () => {
  const runner = await fakeRunner(() => ({ ok: true, result: { run_id: "dr-1", queued: 0, card: CARD } }));
  const agents = await fakeAgents();
  try {
    const self = agent({ thread_id: 7, runtimeArgs: { PLANNER_MODEL: "glm-5.3" } });
    const reply = await runtime.handler.call(self, {
      question: "Bitcoin?",
      depth: "quick",
      sub_questions: '["Price history", {"goal": "Energy use"}]',
      title: "Bitcoin",
    });
    assert.match(reply, /^Deep research started \(run dr-1\)\. .*publishes a cited report to the research site, even if the chat closes\./);
    assert.ok(reply.includes(`\n\nCard: ${CARD}\n\n`));
    assert.match(reply, /Put the Card line in your reply exactly as given/);
    assert.doesNotMatch(reply, /waits for/);
    assert.deepEqual(runner.requests, [
      {
        op: "start",
        args: {
          question: "Bitcoin?", depth: "quick", planner: "glm-5.3", worker: null, planner_fallback: null, site: null,
          sub_questions: ["Price history", { goal: "Energy use" }], title: "Bitcoin",
          // The chat it came from: the runner tells its app when the run ends.
          scope: { workspace: "career", thread: "7" },
        },
      },
    ]);
    // agents-runner tells the chat when it ends, since research-runner can't.
    assert.deepEqual(agents.requests, [
      { op: "follow", args: { run_id: "dr-1", chat: { workspace: "career", thread: 7 }, card: CARD, question: "Bitcoin?" } },
    ]);
    assert.match(reply, /a notice comes back into this chat/);
  } finally {
    await runner.close();
    await agents.close();
  }
});

test("only a chat in AnythingLLM's UI is followed, its main chat as thread null", async () => {
  const runner = await fakeRunner(() => ({ ok: true, result: { run_id: "dr-3", queued: 0, card: CARD } }));
  const agents = await fakeAgents();
  try {
    await runtime.handler.call(agent(), { question: "q" });
    const job = await runtime.handler.call(agent({ chat: false }), { question: "q" });
    const api = await runtime.handler.call(agent({ chat: "api", thread_id: 9 }), { question: "q" });
    assert.deepEqual(
      agents.requests.map((r) => r.args.chat),
      [{ workspace: "career", thread: null }]
    );
    assert.doesNotMatch(job, /notice/);
    assert.doesNotMatch(api, /notice/); // an API chat's thread scopes it, but it isn't told
    assert.deepEqual(runner.requests[1].args.scope, { workspace: "career", thread: "default" });
    assert.deepEqual(runner.requests[2].args.scope, { workspace: "career", thread: "9" });
  } finally {
    await runner.close();
    await agents.close();
  }
});

test("a follow agents-runner can't take still answers with the started run", async () => {
  const runner = await fakeRunner(() => ({ ok: true, result: { run_id: "dr-4", queued: 0, card: CARD } }));
  const logs = [];
  try {
    const self = { ...agent({ thread_id: 7 }), logger: (m) => logs.push(m) };
    const reply = await runtime.handler.call(self, { question: "q" });
    assert.match(reply, /^Deep research started \(run dr-4\)/);
    assert.doesNotMatch(reply, /notice/);
    assert.match(logs.join("\n"), /couldn't have dr-4 followed/);
  } finally {
    await runner.close();
  }
});

test("a queued run says so, and without a card the reply says less", async () => {
  const runner = await fakeRunner(() => ({ ok: true, result: { run_id: "dr-2", queued: 2, card: "" } }));
  try {
    const reply = await runtime.handler.call(agent(), { question: "q" });
    assert.match(reply, /It waits for 2 other research runs to finish first\./);
    assert.doesNotMatch(reply, /Card:/);
    assert.match(reply, /the report will be on the research site/);
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
