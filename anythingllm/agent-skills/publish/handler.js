// Publish: syncs this workspace's /public to the pages site (packages/sandbox), which runs and
// writes there do on their own; with a path outside /public, copies it to /public/<slug>
// first, and with remove, deletes /public's entry for the slug, taking the page down.

const { withSandbox, publishedLines } = require("../_lib/sandbox");

module.exports.runtime = {
  handler: async function ({ path, slug, remove }) {
    return withSandbox(this, async (request) => {
      const r = await request("publish", { slug: slug ?? "", path: path ?? "", remove: remove === true });
      if (r === null) return "The chat closed.";
      if (r.removed) return `removed the page '${r.slug}'`;
      if (r.unchanged) return "/public and the pages site already match; nothing to publish";
      if (!r.url) return publishedLines(r).join("\n");
      const lines = [`published ${r.files} file${r.files === 1 ? "" : "s"}: ${r.url}`];
      if (r.card) lines.push(`Card: ${r.card}`);
      if (r.blocked.length)
        lines.push(`warning: the pages site blocks ${r.blocked.join(", ")}; the page will show without them`);
      return lines.join("\n");
    });
  },
};
