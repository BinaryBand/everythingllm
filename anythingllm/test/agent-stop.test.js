const os = require("os");
const fs = require("fs");
const vm = require("vm");
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const { spawnSync } = require("child_process");

const PRELOAD = path.join(__dirname, "..", "agent-stop.js");
const { patch } = require(PRELOAD);
const REAL = "/app/server/utils/chats/apiChatHandler.js";

// apiChatHandler.js's two agent branches as AnythingLLM 1.17.0 has them, cut down: chatSync
// waits for the whole answer, streamChat streams it to the response.
const HANDLER = `
async function chatSync({ agentHandler, eventListener }) {
  {
    const agentHandler = new EphemeralAgentHandler({});
    agentHandler.startAgentCluster();

    // The cluster has started and now we wait for close event since
    // this is a synchronous call for an agent, so we return everything at once.
    return await eventListener.waitForClose();
  }
}

async function streamChat({ response, agentHandler: given, eventListener }) {
  {
    const agentHandler = new EphemeralAgentHandler(given);

    // Establish event listener that emulates websocket calls
    agentHandler.startAgentCluster();

    // The cluster has started and now we wait for close event since
    // and stream back any results we get from agents as they come in.
    return eventListener
      .streamAgentEvents(response, "uuid")
      .then(() => "saved");
  }
}

function EphemeralAgentHandler(handler) {
  return handler;
}

module.exports = { chatSync, streamChat };
`;

/** Run `node --require agent-stop.js` on a handler saved as .../server/utils/chats/apiChatHandler.js:
 *  streamChat with a response that closes, ended or not, and what the agent was told. */
function load(source, ended) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "agent-stop-"));
  const file = path.join(dir, "server", "utils", "chats", "apiChatHandler.js");
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, source);
  const script = `const { EventEmitter } = require("events");
    const { streamChat } = require(${JSON.stringify(file)});
    const told = [];
    const agentHandler = { startAgentCluster() {}, log: (m) => told.push(m), aibitat: { abort: () => told.push("abort") } };
    const response = new EventEmitter();
    response.writableEnded = ${JSON.stringify(ended)};
    const eventListener = { streamAgentEvents: () => new Promise(() => {}) };
    streamChat({ response, agentHandler, eventListener });
    response.emit("close");
    console.log(JSON.stringify(told));`;
  const run = spawnSync(process.execPath, [`--require=${PRELOAD}`, "-e", script], { encoding: "utf8", env: { ...process.env, NODE_OPTIONS: "" } });
  fs.rmSync(dir, { recursive: true, force: true });
  assert.equal(run.status, 0, run.stderr);
  return { told: JSON.parse(run.stdout), stderr: run.stderr };
}

test("a client that goes before the answer ends stops the agent", () => {
  assert.equal(patch(HANDLER).state, "patched");
  const { told, stderr } = load(HANDLER, false);
  assert.deepEqual(told, ["The client went: stopping the agent.", "abort"]);
  assert.equal(stderr, "");
});

test("a response that closes once it ended leaves the agent alone", () => {
  assert.deepEqual(load(HANDLER, true).told, []);
});

test("only streamChat's branch is patched, on its own line", () => {
  const { source } = patch(HANDLER);
  assert.equal(source.split("\n").length, HANDLER.split("\n").length);
  assert.equal(source.match(/response\.on\("close"/g).length, 1);
  assert.match(source, /agentHandler\.startAgentCluster\(\); response\.on\("close"/);
  assert.match(source, /agentHandler\.startAgentCluster\(\);\n\n    \/\/ The cluster has started and now we wait for close event since\n    \/\/ this is a synchronous/);
});

test("once AnythingLLM stops the agent itself, the file is left alone", () => {
  const fixed = HANDLER.replace("const agentHandler = new EphemeralAgentHandler(given);", "$&\n    abortAgentOnClientDisconnect(response, agentHandler);");
  assert.deepEqual(patch(fixed), { source: fixed, state: "upstream" });
});

test("code that has moved is left alone and said on stderr", () => {
  const moved = HANDLER.replaceAll("startAgentCluster();", "startAgentCluster(true);");
  assert.deepEqual(patch(moved), { source: moved, state: "moved" });
  const branch = HANDLER.slice(HANDLER.indexOf("async function streamChat"), HANDLER.indexOf("function EphemeralAgentHandler"));
  assert.equal(patch(HANDLER + branch.replace("streamChat", "streamChat2")).state, "moved");
  const { told, stderr } = load(moved, false);
  assert.deepEqual(told, []);
  assert.match(stderr, /\[agent-stop\] .*apiChatHandler\.js isn't as expected/);
});

test("the container's own apiChatHandler.js takes the patch and still parses", { skip: !fs.existsSync(REAL) && "not in the AnythingLLM container" }, () => {
  const done = patch(fs.readFileSync(REAL, "utf8"));
  assert.notEqual(done.state, "moved", "AnythingLLM's apiChatHandler.js changed: see agent-stop.js");
  if (done.state === "patched") assert.equal(done.source.match(/response\.on\("close", \(\) => \{ if \(!response\.writableEnded\)/g).length, 1);
  new vm.Script(`(function (exports, require, module, __filename, __dirname) {${done.source}\n})`, { filename: REAL });
});
