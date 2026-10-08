// Show Image: puts an image from the sandbox in the chat (packages/sandbox's show_image). The
// runner copies it to the pages site and gives the Markdown line that shows it.

const { withSandbox } = require("../_lib/sandbox");

module.exports.runtime = {
  handler: async function ({ path, alt }) {
    return withSandbox(this, async (request) => {
      const r = await request("show_image", { path: path ?? "", alt: alt ?? "" });
      const kb = Math.max(1, Math.round(r.bytes / 1024));
      return `shown: ${r.url} (${r.width}×${r.height}, ${kb} KB)\nImage: ${r.image}`;
    });
  },
};
