// Browser Read: the page in this chat's browser tab as it is now (browser-runner,
// packages/browser), or only its lines that contain `find`.

const { withBrowser, say } = require("../_lib/browser");

module.exports.runtime = {
  handler: async function ({ find }) {
    return withBrowser(this, async (request) => {
      say(this, find == null || !String(find).trim() ? "Reading the page" : "Looking for something on the page");
      const r = await request("read", { find: find == null ? "" : String(find) });
      return r === null ? null : r.page;
    });
  },
};
