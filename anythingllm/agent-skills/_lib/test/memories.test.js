// The memories skill: what it sends agents-runner. Its refusal of a delegated task is
// delegated.test.js's, which covers every skill.

const test = require("node:test");
const assert = require("node:assert/strict");
const { fakeService } = require("./fakeservice");

function chat(workspace) {
  return { logger: () => {}, super: { handlerProps: { invocation: { workspace: { slug: workspace }, thread_id: 3 } } } };
}

test("memories sends the invocation's workspace, a list by default, its scope apart from the chat's, and the id as a number", async () => {
  const service = await fakeService((op) => ({ ok: true, result: `${op} ok` }));
  process.env.AGENTS_SOCKET = service.socket;
  try {
    const { handler } = require("../../memories/handler").runtime;
    assert.equal(await handler.call(chat("career"), {}), "memories ok");
    await handler.call(chat("career"), { action: "Save", text: "Lives in Stockholm.", scope: "global", workspace: "x" });
    await handler.call(chat("career"), { action: "forget", id: "12", apply: "true" });
    assert.deepEqual(
      service.requests.map((r) => [r.op, r.args]),
      [
        ["memories", { scope: { workspace: "career", thread: "3" }, action: "list", text: null, memory_scope: null, memory_id: null, apply: false }],
        ["memories", { scope: { workspace: "career", thread: "3" }, action: "save", text: "Lives in Stockholm.", memory_scope: "global", memory_id: null, apply: false }],
        ["memories", { scope: { workspace: "career", thread: "3" }, action: "forget", text: null, memory_scope: null, memory_id: 12, apply: true }],
      ]
    );
  } finally {
    delete process.env.AGENTS_SOCKET;
    await service.close();
  }
});
