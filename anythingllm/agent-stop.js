// Preloaded into AnythingLLM's node processes beside log-filter.js and thread-scope.js
// (NODE_OPTIONS, set in host/quadlet/anythingllm.container.in). AnythingLLM's developer API
// stops a plain chat's answer when the client of `stream-chat` goes (it aborts the
// provider's request), but not an agent's: the agent keeps calling the model and its
// tools, and saves the whole answer to the thread, though the client stopped it (checked in
// 1.16.2 and 1.17.0). So a Stop in a client app stopped nothing but the reading. This
// aborts the agent's session (AIbitat.abort, which the UI's own Stop uses) when the
// response closes before it ended. The aborted session never reaches the code that saves
// it, so a stopped answer isn't kept; a skill call already going finishes, and the agent
// goes no further.
//
// Once AnythingLLM stops the agent itself, this finds that and leaves the file alone; if
// the code it looks for has moved, it says so on stderr and leaves it alone too. Either
// way `uv run hostctl health` says so (`node agent-stop.js --check`): then remove this.
const fs = require("fs");

const TARGET = /[\\/]server[\\/]utils[\\/]chats[\\/]apiChatHandler\.js$/;
const FILE = "/app/server/utils/chats/apiChatHandler.js";
// streamChat's agent branch: the cluster starts, then its events stream to the response.
// chatSync starts one too, but answers at the end and has no response to watch.
const START = /agentHandler\.startAgentCluster\(\);(?=(?:\s*\/\/[^\n]*)*\s*return eventListener\s*\.streamAgentEvents\(response,)/g;
// The same branch, from its handler to its stream, for an abort AnythingLLM added itself.
const BRANCH = /new EphemeralAgentHandler\((?:(?!new EphemeralAgentHandler\()[\s\S])*?\.streamAgentEvents\(response,/g;
const STOPS = /\.abort\(|OnClientDisconnect\(\s*response/;
// On the same line, so the file's line numbers stay as they are.
const STOP =
  ' response.on("close", () => { if (!response.writableEnded) { agentHandler.log("The client went: stopping the agent."); agentHandler.aibitat?.abort?.(); } });';

/** apiChatHandler.js's source with the agent stopped when the client goes, and what was
 *  done: "patched", "upstream" (it stops it already) or "moved" (the code isn't as
 *  expected; left alone). Pure, for the tests and the check. */
function patch(source) {
  if ((source.match(BRANCH) || []).some((branch) => STOPS.test(branch))) return { source, state: "upstream" };
  if ((source.match(START) || []).length !== 1) return { source, state: "moved" };
  return { source: source.replace(START, `$&${STOP}`), state: "patched" };
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
        process.stderr.write(`[agent-stop] ${filename} isn't as expected; a client's Stop won't stop the agent\n`);
      content = done.source;
    }
    return compile.call(this, content, filename, ...rest);
  };
}
