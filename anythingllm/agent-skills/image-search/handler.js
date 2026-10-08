// Image Search: has research-runner (packages/research, research.images) find pictures on
// the web, or fetch the one at a URL, and save copies on the pages site, since the chat
// shows a picture only from the server's own host. Each comes back as an Image: line, a
// Markdown image of our copy linking to the page it's on, for the agent to paste.

const { forward, asInteger } = require("../_lib/runner");

function reply({ images = [], skipped = [] }) {
  const lines = images.map((i) => `Image: ${i.image}\nsource: ${i.source} (${i.width}×${i.height})`);
  if (skipped.length) lines.push(`(${skipped.length} more found couldn't be fetched)`);
  return lines.join("\n");
}

module.exports.runtime = {
  handler: async function ({ query, count, url, alt }) {
    return forward(this, {
      service: "research",
      env: "RESEARCH_SOCKET",
      op: "images",
      args: { query: query ?? "", url: url ?? "", count: asInteger(count) ?? null, alt: alt ?? "" },
      timeoutMs: 60_000,
      reply,
    });
  },
};
