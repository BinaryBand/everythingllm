const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");

const { asObject, asFlag, asInteger, forward } = require("../runner");
const { fakeService } = require("./fakeservice");

const SKILLS = path.join(__dirname, "..", "..");
// Skills that only read, and so may run in a delegated task. None yet: every skill of ours
// writes, acts or delegates.
const READS = new Set([]);

function agent(workspace) {
  return { logger: () => {}, introspect: () => {}, super: { handlerProps: { invocation: { uuid: "inv-1", workspace: { slug: workspace }, thread_id: 3 } } } };
}

test("every skill that writes, acts or delegates refuses a delegated task, before reaching any service", async () => {
  const service = await fakeService(() => ({ ok: true, result: "done" }));
  const envs = ["SANDBOX_SOCKET", "RESEARCH_SOCKET", "AGENTS_SOCKET", "BROWSER_SOCKET"];
  for (const env of envs) process.env[env] = service.socket;
  try {
    const skills = fs.readdirSync(SKILLS).filter((d) => fs.existsSync(path.join(SKILLS, d, "plugin.json")));
    assert.ok(skills.includes("delegate") && skills.includes("run-code"));
    for (const skill of skills) {
      if (READS.has(skill)) continue;
      const { handler } = require(path.join(SKILLS, skill, "handler.js")).runtime;
      const reply = await handler.call(agent("agents-worker"), {});
      assert.match(reply, /^Error: this tool isn't available to a delegated task/, skill);
    }
    assert.deepEqual(service.requests, []);
  } finally {
    for (const env of envs) delete process.env[env];
    await service.close();
  }
});

test("forward sends the op and its args to the service and gives back its text", async () => {
  const service = await fakeService((op, args) =>
    args.slug === "nope" ? { ok: false, error: "there's no page 'nope'" } : { ok: true, result: `${op} ok` }
  );
  const spec = { service: "probe", env: "PROBE_RUNNER", op: "show" };
  process.env.PROBE_RUNNER = service.socket;
  try {
    assert.equal(await forward(agent("career"), { ...spec, args: { slug: "a" } }), "show ok");
    assert.deepEqual(service.requests[0], { op: "show", args: { slug: "a" } });
    assert.equal(await forward(agent("career"), { ...spec, args: { slug: "nope" } }), "Error: there's no page 'nope'");
    // Scheduled jobs have no workspace, and go ahead.
    const job = { logger: () => {}, super: { handlerProps: { invocation: {} } } };
    assert.equal(await forward(job, { ...spec, args: {} }), "show ok");
    // A delegated task doesn't reach the service.
    assert.match(await forward(agent("agents-worker"), { ...spec, args: {} }), /^Error: this tool isn't available to a delegated task/);
    assert.equal(service.requests.length, 3);
  } finally {
    delete process.env.PROBE_RUNNER;
    await service.close();
  }
  process.env.PROBE_RUNNER = "/nonexistent/probe.sock";
  try {
    assert.match(await forward(agent("career"), { ...spec, args: {} }), /probe service isn't running.*uv run hostctl probe-setup/);
  } finally {
    delete process.env.PROBE_RUNNER;
  }
});

test("arguments a model sends as text", () => {
  assert.deepEqual(asObject('{"1": "fix it"}'), { 1: "fix it" });
  assert.equal(asObject(""), null);
  assert.equal(asObject("not json"), "not json");
  assert.equal(asFlag("true"), true);
  assert.equal(asFlag("False"), false);
  assert.equal(asFlag(undefined), null);
  assert.equal(asInteger("7"), 7);
  assert.equal(asInteger("all"), "all");
  assert.equal(asInteger(3), 3);
});

test("delegate starts a delegation and hands back its card", async () => {
  const service = await fakeService((op, args) =>
    op === "delegate" ? { ok: true, result: { run_id: "dg-1", queued: 0, card: "[![D](c.png)](p)" } } : { ok: false, error: "?" }
  );
  process.env.AGENTS_SOCKET = service.socket;
  try {
    const delegate = require("../../delegate/handler").runtime;
    const reply = await delegate.handler.call(agent("career"), {
      goal: "compare",
      tasks: '[{"name": "a", "profile": "worker", "instructions": "x"}]',
    });
    assert.match(reply, /Delegation started \(run dg-1\)[\s\S]*Card: \[!\[D\]\(c\.png\)\]\(p\)/);
    assert.deepEqual(service.requests[0], {
      op: "delegate",
      // The chat it came from, told when it ends.
      args: {
        goal: "compare", tasks: [{ name: "a", profile: "worker", instructions: "x" }], then: null,
        chat: { workspace: "career", thread: 3 },
      },
    });
    assert.match(reply, /a notice comes back into this chat/);
    const job = { logger: () => {}, super: { handlerProps: { invocation: { workspace: { slug: "career" } } } } };
    const unseen = await delegate.handler.call(job, { goal: "g", tasks: "[]" });
    assert.equal(service.requests[1].args.chat, null);
    assert.doesNotMatch(unseen, /notice/);
  } finally {
    delete process.env.AGENTS_SOCKET;
    await service.close();
  }
});

test("update-prompt sends the invocation's workspace, not one the model names", async () => {
  const service = await fakeService((op, args) =>
    op === "update_prompt" ? { ok: true, result: `would update ${args.scope.workspace}` } : { ok: false, error: "?" }
  );
  process.env.AGENTS_SOCKET = service.socket;
  try {
    const update = require("../../update-prompt/handler").runtime;
    const reply = await update.handler.call(agent("career"), { apply: "true", workspace: "education" });
    assert.equal(reply, "would update career");
    assert.deepEqual(service.requests[0], {
      op: "update_prompt",
      args: { scope: { workspace: "career", thread: "3" }, apply: true },
    });
    await update.handler.call(agent("career"), {});
    assert.equal(service.requests[1].args.apply, false);
  } finally {
    delete process.env.AGENTS_SOCKET;
    await service.close();
  }
});
