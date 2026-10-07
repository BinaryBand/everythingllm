// Memories: has agents-runner (packages/agents, agents.memories) list AnythingLLM's saved
// memories for this chat (global and its workspace's), save one, or forget one, showing it
// first. The workspace comes from the invocation, never from the model; a delegated task is
// refused (_lib/delegated.js), and a scheduled job by the runner.

const { forwardScoped, asFlag, asInteger } = require("../_lib/runner");

module.exports.runtime = {
  handler: async function ({ action, text, scope, id, apply }) {
    const memoryId = id == null || id === "" ? null : asInteger(id);
    return forwardScoped(this, {
      service: "agents",
      env: "AGENTS_SOCKET",
      op: "memories",
      args: {
        action: String(action || "list").trim().toLowerCase(),
        text: text == null ? null : String(text),
        memory_scope: scope == null || scope === "" ? null : String(scope),
        memory_id: memoryId,
        apply: asFlag(apply) === true,
      },
    });
  },
};
