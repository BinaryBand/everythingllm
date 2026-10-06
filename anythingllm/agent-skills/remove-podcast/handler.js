// Remove Podcast: unsubscribes through podcasts-runner (packages/podcasts, remove_podcast).
// A skill, not an MCP tool, so that it can refuse a delegated task (_lib/delegated.js).

const { forward } = require("../_lib/runner");

module.exports.runtime = {
  handler: async function ({ slug }) {
    return forward(this, {
      service: "podcasts",
      env: "PODCASTS_SOCKET",
      op: "remove_podcast",
      args: { slug },
    });
  },
};
