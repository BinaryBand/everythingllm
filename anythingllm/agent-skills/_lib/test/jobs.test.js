// The scheduled-jobs, schedule-job and remind-once skills: what they send agents-runner. Their refusal of
// a delegated task is delegated.test.js's, which covers every skill.

const test = require("node:test");
const assert = require("node:assert/strict");
const { fakeService } = require("./fakeservice");

async function fakeAgents() {
  const service = await fakeService((op) => ({ ok: true, result: `${op} ok` }));
  process.env.AGENTS_SOCKET = service.socket;
  return {
    requests: service.requests,
    close: () => {
      delete process.env.AGENTS_SOCKET;
      return service.close();
    },
  };
}

function chat(workspace) {
  return { logger: () => {}, super: { handlerProps: { invocation: { workspace: { slug: workspace }, thread_id: 3 } } } };
}

test("scheduled-jobs sends the invocation's workspace, a list by default, and the id as a number", async () => {
  const agents = await fakeAgents();
  try {
    const { handler } = require("../../scheduled-jobs/handler").runtime;
    assert.equal(await handler.call(chat("career"), {}), "scheduled_jobs ok");
    await handler.call(chat("career"), { action: "Delete", id: "12", apply: "true", workspace: "x" });
    const job = { logger: () => {}, super: { handlerProps: { invocation: {} } } };
    await handler.call(job, { action: "disable", id: 3 });
    assert.deepEqual(
      agents.requests.map((r) => r.args),
      [
        { scope: { workspace: "career", thread: "3" }, action: "list", job_id: null, apply: false },
        { scope: { workspace: "career", thread: "3" }, action: "delete", job_id: 12, apply: true },
        { scope: { workspace: "_jobs", thread: "default" }, action: "disable", job_id: 3, apply: false },
      ]
    );
  } finally {
    await agents.close();
  }
});

test("remind-once sends its tools as a list, whether the model gives a list or JSON text", async () => {
  const agents = await fakeAgents();
  try {
    const { handler } = require("../../remind-once/handler").runtime;
    const base = { name: "stretch", prompt: "Remind the user to stretch.", at: "2026-10-07 14:05" };
    assert.equal(await handler.call(chat("career"), base), "remind_once ok");
    await handler.call(chat("career"), { ...base, tools: '["@@mcp_sites"]', apply: true });
    await handler.call(chat("career"), { ...base, tools: ["web-browsing"], apply: "false" });
    assert.deepEqual(
      agents.requests.map((r) => [r.op, r.args.tools, r.args.apply]),
      [
        ["remind_once", [], false],
        ["remind_once", ["@@mcp_sites"], true],
        ["remind_once", ["web-browsing"], false],
      ]
    );
    assert.deepEqual(agents.requests[0].args, { scope: { workspace: "career", thread: "3" }, ...base, tools: [], apply: false });
  } finally {
    await agents.close();
  }
});

test("schedule-job sends the invocation's workspace, its cron as text and its tools as a list", async () => {
  const agents = await fakeAgents();
  try {
    const { handler } = require("../../schedule-job/handler").runtime;
    const base = { name: "news", prompt: "Summarize the headlines.", schedule: "0 6 * * 1-5" };
    assert.equal(await handler.call(chat("career"), base), "schedule_job ok");
    await handler.call(chat("career"), { ...base, tools: '["@@mcp_sites"]', apply: "true" });
    assert.deepEqual(agents.requests.map((r) => [r.op, r.args.tools, r.args.apply]), [
      ["schedule_job", [], false],
      ["schedule_job", ["@@mcp_sites"], true],
    ]);
    assert.deepEqual(agents.requests[0].args, { scope: { workspace: "career", thread: "3" }, ...base, tools: [], apply: false });
  } finally {
    await agents.close();
  }
});
