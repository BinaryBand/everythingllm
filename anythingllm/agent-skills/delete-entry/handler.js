// Delete Entry: deletes an entry from a Zola site through sites-runner (packages/sites, delete_entry).
// A skill, not an MCP tool, so that it can refuse a delegated task (_lib/delegated.js).

const { forward } = require("../_lib/runner");

module.exports.runtime = {
  handler: async function ({ site, section, slug }) {
    return forward(this, {
      service: "sites",
      env: "SITES_SOCKET",
      op: "delete_entry",
      args: { site, section, slug },
    });
  },
};
