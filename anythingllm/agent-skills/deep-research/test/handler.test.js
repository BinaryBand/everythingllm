const test = require("node:test");
const assert = require("node:assert/strict");
const os = require("os");
const path = require("path");

const { runtime } = require("../handler");
const { fakeService } = require("../../_lib/test/fakeservice");

// Never the live agents-runner: a test that follows a run sets its own.
const NO_AGENTS = path.join(os.tmpdir(), `dr-no-agents-${process.pid}.sock`);
process.env.AGENTS_SOCKET = NO_AGENTS;

// A fake agents-runner, which follows runs for their workspaces and chats.
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
    assert.match(reply, /^Deep research started \(run dr-1\)\. .*writes a cited report, even if the chat closes\./);
    assert.ok(reply.includes(`\n\nCard: ${CARD}\n\n`));
    assert.match(reply, /Put the Card line in your reply exactly as given/);
    assert.doesNotMatch(reply, /waits for/);
    assert.deepEqual(runner.requests, [
      {
        op: "start",
        args: {
          question: "Bitcoin?", depth: "quick", planner: "glm-5.3", worker: null, planner_fallback: null,
          sub_questions: ["Price history", { goal: "Energy use" }], title: "Bitcoin",
          // The chat it came from: the runner tells its app when the run ends.
          scope: { workspace: "career", thread: "7" },
        },
      },
    ]);
    // agents-runner keeps the report and tells the chat when it ends, since research-runner can't.
    assert.deepEqual(agents.requests, [
      {
        op: "follow",
        args: { run_id: "dr-1", chat: { workspace: "career", thread: 7 }, card: CARD, question: "Bitcoin?", workspace: "career" },
      },
    ]);
    assert.match(reply, /the report goes into this workspace's documents/);
    assert.match(reply, /a notice comes back into this chat/);
  } finally {
    await runner.close();
    await agents.close();
  }
});

test("a run from any of a workspace's chats is followed, and only a UI chat is told", async () => {
  const runner = await fakeRunner(() => ({ ok: true, result: { run_id: "dr-3", queued: 0, card: CARD } }));
  const agents = await fakeAgents();
  try {
    await runtime.handler.call(agent(), { question: "q" });
    const job = await runtime.handler.call(agent({ chat: false, workspace: null }), { question: "q" });
    const api = await runtime.handler.call(agent({ chat: "api", thread_id: 9 }), { question: "q" });
    // The main chat as thread null; an API chat for its workspace, with no chat to tell.
    assert.deepEqual(
      agents.requests.map((r) => [r.args.workspace, r.args.chat]),
      [
        ["career", { workspace: "career", thread: null }],
        ["career", null],
      ]
    );
    assert.doesNotMatch(job, /notice/);
    assert.match(job, /saved in the agent's files, in research\//); // a job has no workspace
    assert.doesNotMatch(api, /notice/); // an API chat's thread scopes it, but it isn't told
    assert.match(api, /the report goes into this workspace's documents/);
    assert.deepEqual(runner.requests[1].args.scope, { workspace: "_jobs", thread: "default" });
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
    assert.match(reply, /saved in the agent's files/); // no agents-runner to follow it
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
