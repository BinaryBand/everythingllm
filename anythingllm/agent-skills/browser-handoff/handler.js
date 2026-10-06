// Browser Handoff: gives the user this workspace's browser, to log in, pass a 2FA check or
// a CAPTCHA, or do anything the agent shouldn't (browser-runner, packages/browser); with
// done, takes it back once they've said in the chat that they're finished. Handing it over
// returns at once: the agent puts the card in its reply and ends it, since the user can only
// see the card once the reply is out.

const { withBrowser, cardLines } = require("../_lib/browser");
const { asFlag } = require("../_lib/runner");

module.exports.runtime = {
  handler: async function ({ reason, done }) {
    return withBrowser(this, async (request) => {
      if (asFlag(done)) {
        const r = await request("handoff", { done: true });
        if (r === null) return null;
        return ["You have the browser again.", r.page].filter(Boolean).join("\n");
      }
      const why = reason == null ? "" : String(reason).trim();
      const r = await request("handoff", { reason: why });
      if (r === null) return null;
      const where = r.card ? cardLines(r.card) : [`The browser for the user: ${r.takeover}`, ""];
      return [
        ...where,
        "The user has the browser now. In your reply, say in a sentence what they should do in it" +
          (why ? ` (${why})` : "") +
          ', that they open it from the card, and that they press "Hand back to the agent" (or tell you) when ' +
          "they're done. Then end your reply: your browser actions are refused until they hand it back. " +
          "If they tell you in the chat that they're done, call browser-handoff with done: true.",
      ].join("\n");
    });
  },
};
