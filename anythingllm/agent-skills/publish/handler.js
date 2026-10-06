// Publish: a page's address and link card (packages/sandbox). This workspace's /public is its
// pages, live as they're written; with a path outside /public, publish copies it to
// /public/<slug> first, and with remove, deletes the page from /public.

const { withSandbox } = require("../_lib/sandbox");

module.exports.runtime = {
  handler: async function ({ path, slug, remove }) {
    return withSandbox(this, async (request) => {
      const r = await request("publish", { slug: slug ?? "", path: path ?? "", remove: remove === true });
      if (r === null) return "The chat closed.";
      if (r.removed) return `removed /public/${r.slug}; it's no longer on the web`;
      if (r.pages)
        return r.pages.length
          ? [`This workspace's pages (${r.site}):`, ...r.pages.map((p) => `- ${p.slug}: ${p.url}`)].join("\n")
          : `This workspace has no pages yet; whatever goes in /public is live at ${r.site}`;
      const lines = [`live: ${r.url} (${r.files} file${r.files === 1 ? "" : "s"})`];
      if (r.card) lines.push(`Card: ${r.card}`);
      if (r.blocked?.length)
        lines.push(`warning: the pages site blocks ${r.blocked.join(", ")}; the page will show without them`);
      return lines.join("\n");
    });
  },
};
