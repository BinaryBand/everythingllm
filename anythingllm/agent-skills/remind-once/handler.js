// Remind Once: has agents-runner (packages/agents, agents.jobs) make a one-off scheduled
// job at a local date-time, shown first and made only with apply, and delete it once it
// has run. A delegated task is refused (_lib/delegated.js), and a scheduled job by the runner.

const { forwardScoped, asFlag, asObject } = require("../_lib/runner");

module.exports.runtime = {
  handler: async function ({ name, prompt, at, tools, apply }) {
    return forwardScoped(this, {
      service: "agents",
      env: "AGENTS_SOCKET",
      op: "remind_once",
      args: {
        name: name ?? "",
        prompt: prompt ?? "",
        at: at == null ? "" : String(at),
        tools: asObject(tools) ?? [],
        apply: asFlag(apply) === true,
      },
    });
  },
};
