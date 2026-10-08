// Memories: has agents-runner (packages/agents, agents.memories) list AnythingLLM's saved
// memories for this chat (global and its workspace's), save one, or forget one. The
// workspace comes from the invocation, never from the model; a delegated task is refused
// (_lib/delegated.js), and a scheduled job by the runner, which also checks the rest.

const { forwardScoped } = require("../_lib/runner");

module.exports.runtime = {
  handler: async function ({ action, text, scope, id }) {
    return forwardScoped(this, {
      service: "agents",
      op: "memories",
      args: {
        action: String(action || "list").trim().toLowerCase(),
        text,
        memory_scope: scope,
        memory_id: id,
      },
    });
  },
};
