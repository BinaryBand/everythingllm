// Run Job: starts a scheduled job now through audit-runner (packages/audit, run_job).
// A skill, not an MCP tool, so that it can refuse a delegated task (_lib/delegated.js).

const { forward } = require("../_lib/runner");

module.exports.runtime = {
  handler: async function ({ name }) {
    return forward(this, {
      service: "audit",
      env: "AUDIT_SOCKET",
      op: "run_job",
      args: { name },
    });
  },
};
