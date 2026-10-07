// Browser Login: uses a login saved in this workspace's vault (browser-runner, packages/browser)
// without ever seeing it: lists the logins (site and username only), or has the runner fill
// one, or its current 2FA code, into fields on the login's own site. A login the user marked
// "ask me first" waits here for their OK in the take-over view, which the chat's card opens.
// Without one, `ask` gives the agent a card for its reply that opens a form for the user's
// login for the site of the chat's page, which goes into the vault.

const { withBrowser } = require("../_lib/browser");
const { asFlag } = require("../_lib/runner");

const MAX_WAIT_MS = 5 * 60_000;

function listing(r) {
  if (!r.logins.length)
    return "No logins are saved in this workspace. Ask the user for one with action ask (on the site's sign-in page), or hand the browser to them with browser-handoff.";
  const lines = r.logins.map(
    (l) =>
      `- ${l.id}: ${l.site}${l.username ? ` as ${l.username}` : ""}${l.totp ? ", with 2FA codes" : ""}${l.ask ? ", asks the user first" : ""}${l.here ? " (fits this page)" : ""}`
  );
  return [`Saved logins${r.site ? ` (this chat's page is on ${r.site})` : ""}:`, ...lines].join("\n");
}

module.exports.runtime = {
  handler: async function ({ action, login, user_ref, pass_ref, ref, submit }) {
    return withBrowser(this, async (request) => {
      const what = String(action || "list");
      if (what === "list") {
        const r = await request("logins", {});
        return r === null ? null : listing(r);
      }
      if (what === "ask") {
        const r = await request("ask_login", {});
        if (r === null) return null;
        return [
          r.card ? `Card: ${r.card}` : "",
          r.card
            ? `Put the Card line in your reply exactly as given, on its own line, ask the user to open it and save their ${r.site} login there (never in the chat), and end your reply. When they say it's saved, list the logins and log in with it.`
            : `Ask the user to save their ${r.site} login in the browser's take-over view (Saved logins), never in the chat, and end your reply.`,
        ].filter(Boolean).join("\n");
      }
      if (what !== "login" && what !== "code") return `Error: action is list, ask, login or code, not '${what}'.`;
      const args =
        what === "login"
          ? { login: String(login ?? ""), user_ref: String(user_ref ?? ""), pass_ref: String(pass_ref ?? ""), submit: asFlag(submit) === true }
          : { login: String(login ?? ""), ref: String(ref ?? ""), submit: asFlag(submit) === true };
      let r = await request(what, args);
      if (r === null) return null;
      if (r.approval) {
        const until = Date.now() + MAX_WAIT_MS;
        this.introspect("Waiting for your OK in the browser (open it from this chat's browser card) to use that saved login…");
        let w = { done: false };
        while (!w.done && Date.now() < until) {
          w = await request("wait_approval", { approval: r.approval });
          if (w === null) return null;
        }
        if (!w.done)
          return [
            r.card ? `Card: ${r.card}` : "",
            "The user hasn't answered yet. Put the Card line in your reply, ask them to allow the saved login in the browser view, and end your reply.",
          ].filter(Boolean).join("\n");
        if (w.stale)
          return "That request for the user's OK was replaced (by another saved login's, or the browser restarted). Call browser-login again.";
        if (!w.approved) return "The user didn't allow that login. Ask them what they'd like instead.";
        r = await request(what, args);
        if (r === null) return null;
        if (r.approval) return "The user's OK didn't stick; ask them to try again.";
      }
      return r.page;
    });
  },
};
