// Preloaded into AnythingLLM's node processes beside log-filter.js (NODE_OPTIONS, set in
// host/quadlet/anythingllm.container.in). AnythingLLM's EphemeralAgentHandler, the agent
// behind developer-API and Telegram chats, holds the chat's thread but leaves thread_id out
// of the invocation it hands skills (handlerProps.invocation; checked in 1.16.2), so to a
// skill every such chat in a workspace looks like the workspace's main chat: one browser tab
// and card, one sandbox threads/default/. This adds it as ephemeral.js is loaded, as the
// upstream fix would (docs/.notes/anythingllm-thread-scope.md).
//
// Once AnythingLLM passes it itself, this finds thread_id there and leaves the file alone;
// if the code it looks for has moved, it says so on stderr and leaves it alone too. Either
// way `uv run hostctl health` says so (`node thread-scope.js --check`): then remove this.
const fs = require("fs");

const TARGET = /[\\/]server[\\/]utils[\\/]agents[\\/]ephemeral\.js$/;
const FILE = "/app/server/utils/agents/ephemeral.js";
const INVOCATION = /(invocation:\s*\{\s*workspace:\s*this\.#workspace,\s*workspace_id:\s*this\.#workspace\?\.id\s*\?\?\s*null,)(\s*\})/;
const PASSED = /invocation:\s*\{[^}]*\bthread_id\s*:/;

/** ephemeral.js's source with thread_id in its invocation, and what was done: "patched",
 *  "upstream" (it has one already) or "moved" (the code isn't as expected; left alone).
 *  Pure, for the tests and the check. */
function patch(source) {
  if (PASSED.test(source)) return { source, state: "upstream" };
  if (!INVOCATION.test(source) || !/#threadId\b/.test(source)) return { source, state: "moved" };
  return { source: source.replace(INVOCATION, "$1 thread_id: this.#threadId ?? null,$2"), state: "patched" };
}

module.exports = { patch };

if (require.main === module && process.argv.includes("--check")) {
  console.log(patch(fs.readFileSync(FILE, "utf8")).state);
} else if (!require.main) {
  // Preloaded (no main module yet). A fork (Bree's scheduled jobs) inherits execArgv.
  const self = `--require=${__filename}`;
  if (!process.execArgv.includes(self)) process.execArgv.push(self);
  const Module = require("module");
  const compile = Module.prototype._compile;
  Module.prototype._compile = function (content, filename, ...rest) {
    if (typeof content === "string" && TARGET.test(filename)) {
      const done = patch(content);
      if (done.state === "moved")
        process.stderr.write(`[thread-scope] ${filename} isn't as expected; API and Telegram chats share their workspace's scope\n`);
      content = done.source;
    }
    return compile.call(this, content, filename, ...rest);
  };
}
