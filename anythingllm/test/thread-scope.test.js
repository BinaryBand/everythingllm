const fs = require("fs");
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const { runPreloaded, parsesAsModule } = require("./preload");

const PRELOAD = path.join(__dirname, "..", "thread-scope.js");
const { patch } = require(PRELOAD);
const REAL = "/app/server/utils/agents/ephemeral.js";

// ephemeral.js's invocation as AnythingLLM 1.16.2 has it, in a class that hands it back.
const HANDLER = `
class EphemeralAgentHandler {
  #workspace = { slug: "career", id: 3 };
  #threadId = null;
  constructor({ threadId = null } = {}) { this.#threadId = threadId; }
  createAIbitat() {
    return {
      handlerProps: {
        invocation: {
          workspace: this.#workspace,
          workspace_id: this.#workspace?.id ?? null,
        },
        log: () => {},
      },
    };
  }
}
module.exports = { EphemeralAgentHandler };
`;

/** Run `node --require thread-scope.js` on a handler saved as .../server/utils/agents/ephemeral.js. */
function load(source, threadId) {
  const { out, stderr } = runPreloaded(PRELOAD, "server/utils/agents/ephemeral.js", source, (file) => `const { EphemeralAgentHandler: H } = require(${JSON.stringify(file)});
    console.log(JSON.stringify(new H({ threadId: ${JSON.stringify(threadId)} }).createAIbitat().handlerProps.invocation))`);
  return { invocation: out, stderr };
}

test("an API or Telegram chat's invocation gets its thread, and a chat with none gets null", () => {
  assert.equal(patch(HANDLER).state, "patched");
  const { invocation, stderr } = load(HANDLER, 42);
  assert.deepEqual(invocation, { workspace: { slug: "career", id: 3 }, workspace_id: 3, thread_id: 42 });
  assert.equal(stderr, "");
  assert.equal(load(HANDLER, null).invocation.thread_id, null);
});

test("once AnythingLLM passes the thread itself, the file is left alone", () => {
  const fixed = HANDLER.replace("workspace_id: this.#workspace?.id ?? null,", "$&\n          thread_id: this.#threadId,");
  assert.deepEqual(patch(fixed), { source: fixed, state: "upstream" });
  assert.equal(load(fixed, 7).invocation.thread_id, 7);
});

test("code that has moved is left alone and said on stderr", () => {
  const moved = HANDLER.replace("workspace_id: this.#workspace?.id ?? null,", "workspaceId: this.#workspace?.id,");
  assert.deepEqual(patch(moved), { source: moved, state: "moved" });
  const { invocation, stderr } = load(moved, 42);
  assert.equal(invocation.thread_id, undefined);
  assert.match(stderr, /\[thread-scope\] .*ephemeral\.js isn't as expected/);
});

test("the container's own ephemeral.js takes the patch and still parses", { skip: !fs.existsSync(REAL) && "not in the AnythingLLM container" }, () => {
  const done = patch(fs.readFileSync(REAL, "utf8"));
  assert.notEqual(done.state, "moved", "AnythingLLM's ephemeral.js changed: see thread-scope.js");
  if (done.state === "patched") assert.match(done.source, /workspace_id: this\.#workspace\?\.id \?\? null, thread_id: this\.#threadId \?\? null,/);
  parsesAsModule(done.source, REAL);
});
