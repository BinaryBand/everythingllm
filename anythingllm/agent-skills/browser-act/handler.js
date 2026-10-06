// Browser Act: one action in this chat's browser tab (browser-runner, packages/browser):
// click, type, choose, scroll, go back… on an element by its ref from the last read, and
// replies with the page as it is after.

const { withBrowser } = require("../_lib/browser");

module.exports.runtime = {
  handler: async function ({ action, ref, text }) {
    return withBrowser(this, async (request) => {
      const r = await request("act", {
        action: String(action ?? ""),
        ref: ref == null ? "" : String(ref).trim(),
        text: text == null ? "" : String(text),
      });
      return r === null ? null : r.page;
    });
  },
};
