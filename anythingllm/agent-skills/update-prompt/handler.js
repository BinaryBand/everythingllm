// Update Prompt: has agents-runner (packages/agents) refresh this workspace's EverythingLLM
// block in its system prompt from the repo's (hostenv.prompt), keeping the rest. The
// workspace comes from the invocation, never from the model; a delegated task is refused
// (_lib/delegated.js), and a scheduled job, which has no workspace, by the runner.

const { forwardScoped, asFlag } = require("../_lib/runner");

module.exports.runtime = {
  handler: async function ({ apply }) {
    return forwardScoped(this, {
      service: "agents",
      op: "update_prompt",
      args: { apply: asFlag(apply) === true },
    });
  },
};
