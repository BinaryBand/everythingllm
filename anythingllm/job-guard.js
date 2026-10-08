// Preloaded into AnythingLLM's node processes beside the other preloads (NODE_OPTIONS, set in
// host/quadlet/anythingllm.container.in). A delegated task runs as AnythingLLM's own agent in
// an `agents-*` workspace, where every tool loads, AnythingLLM's own create-scheduled-job
// among them, which our skills' refusal of a delegated task (agent-skills/_lib/delegated.js)
// doesn't reach; and a job runs later with every tool approved. This gives that tool the
// same refusal, as its file loads: a delegated task can't make a job, and nothing else is
// held back, a job made in the UI meanwhile included.
//
// If the code it looks for has moved, it says so on stderr and leaves the file alone, and
// `uv run hostctl health` says so (`node job-guard.js --check`).
const path = require("path");
const { patchOnLoad } = require("./patch-on-load");

const TARGET = /[\\/]server[\\/]utils[\\/]agents[\\/]aibitat[\\/]plugins[\\/]create-scheduled-job[\\/]index\.js$/;
const FILE = "/app/server/utils/agents/aibitat/plugins/create-scheduled-job/index.js";
const HANDLER = "handler: async function (args = {}) {";
const DELEGATED = path.join(__dirname, "agent-skills", "_lib", "delegated.js");

/** The plugin's source with its handler refusing a delegated task (through `delegated`, the
 *  skills' own refusal), and what was done: "patched", or "moved" (the code isn't as
 *  expected; left alone). On the handler's own line, so the file's line numbers stay as
 *  they are. Pure, for the tests and the check. */
function patch(source, delegated = DELEGATED) {
  if (source.split(HANDLER).length !== 2) return { source, state: "moved" };
  const guard = ` const refused = require(${JSON.stringify(delegated)}).delegatedRefusal(this); if (refused) return refused;`;
  return { source: source.replace(HANDLER, `${HANDLER}${guard}`), state: "patched" };
}

module.exports = { patch };

patchOnLoad(module, { file: FILE, target: TARGET, patch, tag: "job-guard", moved: "a delegated task can make a scheduled job" });
