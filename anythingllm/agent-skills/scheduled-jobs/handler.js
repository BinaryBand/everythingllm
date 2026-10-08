// Scheduled Jobs: has agents-runner (packages/agents, agents.jobs) list AnythingLLM's
// scheduled jobs, or delete or disable one the repo doesn't manage, showing it first. The
// workspace comes from the invocation, never from the model; a delegated task is refused
// (_lib/delegated.js), and a scheduled job by the runner.

const { forwardScoped, asFlag, asInteger } = require("../_lib/runner");

module.exports.runtime = {
  handler: async function ({ action, id, apply }) {
    const jobId = id == null || id === "" ? null : asInteger(id);
    return forwardScoped(this, {
      service: "agents",
      op: "scheduled_jobs",
      args: {
        action: String(action || "list").trim().toLowerCase(),
        job_id: jobId,
        apply: asFlag(apply) === true,
      },
    });
  },
};
