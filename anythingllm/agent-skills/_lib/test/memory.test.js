// The long-memory skill: what it sends memory-runner, and that it gives back the answer's
// text. Its refusal of a delegated task is delegated.test.js's, which covers every skill.

const test = require("node:test");
const assert = require("node:assert/strict");
const { fakeService } = require("./fakeservice");

function chat(workspace) {
  return { logger: () => {}, super: { handlerProps: { invocation: { workspace: { slug: workspace }, thread_id: 3 } } } };
}

test("long-memory sends the invocation's scope, recall by default, and each action's own args", async () => {
  const service = await fakeService((op) => ({ ok: true, result: { text: `${op} ok`, hits: [] } }));
  process.env.MEMORY_SOCKET = service.socket;
  try {
    const { handler } = require("../../long-memory/handler").runtime;
    const home = chat("home");
    assert.equal(await handler.call(home, { query: "coffee" }), "recall ok");
    await handler.call(home, { action: "Remember", text: "Takes coffee black.", scope: "global", workspace: "x" });
    await handler.call(home, { action: "forget", id: 12 });
    await handler.call(home, { action: "ingest", path: "/project/notes, /work/a.txt" });
    await handler.call(home, { action: "recall", query: "pump", k: "3", only: "files" });
    const scope = { workspace: "home", thread: "3" };
    assert.deepEqual(
      service.requests.map((r) => [r.op, r.args]),
      [
        ["recall", { query: "coffee", k: null, only: null, scope }],
        ["remember", { text: "Takes coffee black.", memory_scope: "global", scope }],
        ["forget", { id: 12, path: null, scope }],
        ["ingest", { paths: ["/project/notes", "/work/a.txt"], scope }],
        ["recall", { query: "pump", k: 3, only: "files", scope }],
      ]
    );
    assert.match(await handler.call(home, { action: "save" }), /^Error: action is one of recall, remember/);
    assert.equal(service.requests.length, 5);
  } finally {
    delete process.env.MEMORY_SOCKET;
    await service.close();
  }
});
