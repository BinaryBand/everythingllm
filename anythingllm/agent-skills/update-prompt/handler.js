// Update Prompt: has agents-runner (packages/agents) refresh this workspace's EverythingLLM
// block in its system prompt from the repo's (hostctl.prompt), keeping the rest. The
// workspace comes from the invocation, never from the model; a delegated task is refused
// (_lib/delegated.js), and a scheduled job, which has no workspace, by the runner.

const { forward, asFlag } = require("../_lib/runner");
const { scopeOf } = require("../_lib/scope");

module.exports.runtime = {
  handler: async function ({ apply }) {
    return forward(this, {
      service: "agents",
      env: "AGENTS_SOCKET",
      op: "update_prompt",
      args: { scope: scopeOf(this), apply: asFlag(apply) === true },
    });
  },
};
