// Browser Act: one action in this chat's browser tab (browser-runner, packages/browser):
// click, type, choose, scroll, go back… on an element by its ref from the last read, and
// replies with the page as it is after.

const { withBrowser, say, actLine } = require("../_lib/browser");
const { Closed } = require("../_lib/hostrpc");

const NAMED = new Set(["click", "fill", "type", "select", "check", "uncheck", "hover"]);

module.exports.runtime = {
  handler: async function ({ action, ref, text }) {
    return withBrowser(this, async (request) => {
      const args = {
        action: String(action ?? ""),
        ref: ref == null ? "" : String(ref).trim(),
        text: text == null ? "" : String(text),
      };
      // The element's name from the last read: the runner has it, the call has only its ref.
      // Only for the line, so a runner that can't say (one from before `label`) isn't a failure.
      let label = "";
      if (NAMED.has(args.action) && args.ref) {
        try {
          label = (await request("label", { ref: args.ref })).label;
        } catch (e) {
          if (e instanceof Closed) throw e;
        }
      }
      say(this, actLine(args.action, label, args.ref, args.text));
      return (await request("act", args)).page;
    });
  },
};
