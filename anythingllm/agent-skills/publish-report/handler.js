// Publish Report: publishes the day's audit report through audit-runner (packages/audit, publish_report).
// A skill, not an MCP tool, so that it can refuse a delegated task (_lib/delegated.js).

const { forward, asObject } = require("../_lib/runner");

module.exports.runtime = {
  handler: async function ({ summary, suggestions, status }) {
    return forward(this, {
      service: "audit",
      env: "AUDIT_SOCKET",
      op: "publish_report",
      args: { summary, suggestions: asObject(suggestions), status: status || null },
    });
  },
};
