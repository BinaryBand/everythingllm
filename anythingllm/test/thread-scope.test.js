const os = require("os");
const fs = require("fs");
const vm = require("vm");
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const { spawnSync } = require("child_process");

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
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "thread-scope-"));
  const file = path.join(dir, "server", "utils", "agents", "ephemeral.js");
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, source);
  const script = `const { EphemeralAgentHandler: H } = require(${JSON.stringify(file)});
    console.log(JSON.stringify(new H({ threadId: ${JSON.stringify(threadId)} }).createAIbitat().handlerProps.invocation))`;
  const run = spawnSync(process.execPath, [`--require=${PRELOAD}`, "-e", script], { encoding: "utf8", env: { ...process.env, NODE_OPTIONS: "" } });
  fs.rmSync(dir, { recursive: true, force: true });
  assert.equal(run.status, 0, run.stderr);
  return { invocation: JSON.parse(run.stdout), stderr: run.stderr };
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
  new vm.Script(`(function (exports, require, module, __filename, __dirname) {${done.source}\n})`, { filename: REAL });
});
