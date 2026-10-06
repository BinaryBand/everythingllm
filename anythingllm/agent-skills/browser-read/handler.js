// Browser Read: the page in this chat's browser tab as it is now (browser-runner,
// packages/browser), or only its lines that contain `find`.

const { withBrowser } = require("../_lib/browser");

module.exports.runtime = {
  handler: async function ({ find }) {
    return withBrowser(this, async (request) => {
      const r = await request("read", { find: find == null ? "" : String(find) });
      return r === null ? null : r.page;
    });
  },
};
