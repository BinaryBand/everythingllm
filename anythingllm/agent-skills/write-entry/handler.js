// Write Entry: saves an entry on a Zola site through sites-runner (packages/sites, write_entry).
// A skill, not an MCP tool, so that it can refuse a delegated task (_lib/delegated.js).

const { forward, asObject, asFlag } = require("../_lib/runner");

module.exports.runtime = {
  handler: async function ({ site, section, slug, title, date, extra, body, overwrite }) {
    return forward(this, {
      service: "sites",
      env: "SITES_SOCKET",
      op: "write_entry",
      args: { site, section, slug, title, date, extra: asObject(extra) ?? {}, body: body ?? "", overwrite: asFlag(overwrite) === true },
    });
  },
};
