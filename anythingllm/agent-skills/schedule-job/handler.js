// Schedule Job: has agents-runner (packages/agents, agents.jobs) make a recurring scheduled
// job on a UTC cron, shown first and made only with apply. It stands in for AnythingLLM's
// own create-scheduled-job, which is turned off: a job runs with every tool approved, so a
// delegated task is refused (_lib/delegated.js), and a scheduled job by the runner.

const { forwardScoped, asFlag, asObject } = require("../_lib/runner");

module.exports.runtime = {
  handler: async function ({ name, prompt, schedule, tools, apply }) {
    return forwardScoped(this, {
      service: "agents",
      env: "AGENTS_SOCKET",
      op: "schedule_job",
      args: {
        name: name ?? "",
        prompt: prompt ?? "",
        schedule: schedule == null ? "" : String(schedule),
        tools: asObject(tools) ?? [],
        apply: asFlag(apply) === true,
      },
    });
  },
};
