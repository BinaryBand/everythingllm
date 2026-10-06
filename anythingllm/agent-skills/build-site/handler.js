// Build Site: builds a Zola site from this workspace's sandbox folders in sandbox-runner
// (packages/sandbox), with no network and its theme from the repo or another workspace's
// /shared, into /public/<slug>, which puts it live. Waits for the build like run-code.

const { withSandbox, publishedLines } = require("../_lib/sandbox");

module.exports.runtime = {
  handler: async function ({ path, slug }) {
    return withSandbox(this, async (request) => {
      let r = await request("build_site", { path: path ?? "", slug: slug ?? "" });
      while (r?.running) {
        this.introspect(`Still building (${Math.round(r.seconds)} s)…`);
        r = await request("wait", { run_id: r.run_id });
      }
      if (r === null) return "The chat closed; the build carries on, and the site goes live when it's done.";
      const lines = [`built ${r.files} file${r.files === 1 ? "" : "s"} into /public/${r.slug}`];
      lines.push(...(r.published ? publishedLines(r.published) : [`live: ${r.url}`]));
      return lines.join("\n");
    });
  },
};
