// Browse: opens an address in this chat's tab of the workspace's browser (browser-runner,
// packages/browser) and replies with the page as text, with the tab's live card the first
// time it's opened.

const { withBrowser, cardLines, say, hostOf } = require("../_lib/browser");

module.exports.runtime = {
  handler: async function ({ url }) {
    return withBrowser(this, async (request) => {
      const host = hostOf(url);
      say(this, host ? `Opening ${host}` : "Opening a page");
      const r = await request("open", { url: String(url ?? "") });
      if (r === null) return null;
      return [...(r.new ? cardLines(r.card) : []), r.page].join("\n");
    });
  },
};
