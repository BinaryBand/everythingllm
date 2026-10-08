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
const { patchOnLoad } = require("./patch-on-load");

const TARGET = /[\\/]server[\\/]utils[\\/]chats[\\/]apiChatHandler\.js$/;
const FILE = "/app/server/utils/chats/apiChatHandler.js";
// An agent branch runs from its handler on; streamChat's is the one whose events stream to
// the response (chatSync's answers at the end, and has no response to watch).
const HANDLER = "new EphemeralAgentHandler(";
const STREAM = ".streamAgentEvents(response,";
const START = "agentHandler.startAgentCluster();";
const STOPS = /\.abort\(|OnClientDisconnect\(\s*response/;
// On the same line, so the file's line numbers stay as they are.
const STOP =
  ' response.on("close", () => { if (!response.writableEnded) { agentHandler.log("The client went: stopping the agent."); agentHandler.aibitat?.abort?.(); } });';

/** apiChatHandler.js's source with the agent stopped when the client goes, and what was
 *  done: "patched", "upstream" (it stops it already) or "moved" (the code isn't as
 *  expected; left alone). Pure, for the tests and the check. */
function patch(source) {
  const parts = source.split(HANDLER);
  const streaming = parts.flatMap((part, i) => (i > 0 && part.includes(STREAM) ? [i] : []));
  const branch = (i) => parts[i].slice(0, parts[i].indexOf(STREAM));
  if (streaming.some((i) => STOPS.test(branch(i)))) return { source, state: "upstream" };
  if (streaming.length !== 1 || branch(streaming[0]).split(START).length !== 2) return { source, state: "moved" };
  const i = streaming[0];
  parts[i] = parts[i].replace(START, `${START}${STOP}`);
  return { source: parts.join(HANDLER), state: "patched" };
}

module.exports = { patch };

patchOnLoad(module, { file: FILE, target: TARGET, patch, tag: "agent-stop", moved: "a client's Stop won't stop the agent" });
