// Browser Read: the page in this chat's browser tab as it is now (browser-runner,
// packages/browser), or only its lines that contain `find`; with card, the tab's live card
// too, for a user who asks to see the browser.

const { withBrowser, cardLines, say } = require("../_lib/browser");
const { asFlag } = require("../_lib/runner");

module.exports.runtime = {
  handler: async function ({ find, card }) {
    return withBrowser(this, async (request) => {
      const showing = asFlag(card);
      say(this, showing ? "Getting the browser's card" : find == null || !String(find).trim() ? "Reading the page" : "Looking for something on the page");
      const r = await request("read", { find: find == null ? "" : String(find), ...(showing ? { card: true } : {}) });
      return showing ? [...cardLines(r.card), r.page].join("\n") : r.page;
    });
  },
};
