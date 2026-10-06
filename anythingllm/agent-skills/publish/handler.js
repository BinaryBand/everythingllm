// Publish: copies a file or folder from the sandbox (packages/sandbox) to `/<slug>/` on the
// pages site, as a page that belongs to this workspace, or takes one down.

const { withSandbox } = require("../_lib/sandbox");

module.exports.runtime = {
  handler: async function ({ path, slug, remove }) {
    return withSandbox(this, async (request) => {
      const r = await request("publish", { slug: slug ?? "", path: path ?? "", remove: remove === true });
      if (r === null) return "The chat closed.";
      if (r.removed) return `removed the page '${r.slug}'`;
      const lines = [`published ${r.files} file${r.files === 1 ? "" : "s"}: ${r.url}`];
      if (r.card) lines.push(`Card: ${r.card}`);
      if (r.blocked.length)
        lines.push(`warning: the pages site blocks ${r.blocked.join(", ")}; the page will show without them`);
      return lines.join("\n");
    });
  },
};
