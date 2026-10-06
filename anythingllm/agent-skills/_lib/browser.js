// What the browser skills (browse, browser-act, browser-read, browser-handoff,
// browser-login) share: the runner's socket, the call's scope (_lib/scope.js: browser-runner
// keeps a browser per workspace and a tab per chat thread by it), and turning the runner's
// errors into replies.

const { call, socketPath, Down, Refused } = require("./hostrpc");
const { delegatedRefusal } = require("./delegated");
const { scopeOf } = require("./scope");

// Starting a workspace's browser and loading a page can take a while; the runner's own
// limits come first.
const TIMEOUT_MS = 120_000;
const CLOSED = "The chat closed before the browser answered.";

/**
 * Run `work(request)` for a skill, where request(op, args) calls browser-runner with the
 * call's scope added. Never throws (a skill that throws ends the chat): a failure becomes
 * the reply, and a closed chat resolves request() with null.
 */
async function withBrowser(self, work) {
  const refused = delegatedRefusal(self);
  if (refused) return refused;
  const signal = self.super?.abortController?.signal ?? null;
  const scope = scopeOf(self);
  const request = (op, args) =>
    call(socketPath("browser", "BROWSER_SOCKET"), op, { scope, ...args }, { name: "the browser runner", signal, timeoutMs: TIMEOUT_MS });
  try {
    return (await work(request)) ?? CLOSED;
  } catch (e) {
    self.logger?.(`browser: ${e?.message || e}`);
    if (e instanceof Down)
      return `The browser service isn't running on the server (${e.message}). Tell the user it needs \`uv run hostctl browser-setup\`.`;
    if (e instanceof Refused) return `Error: ${e.message}`;
    return `The browser failed: ${e?.message || e}`;
  }
}

/** The lines that hand the agent a tab's live card. */
function cardLines(card) {
  if (!card) return [];
  return [
    `Card: ${card}`,
    "Put the Card line in your reply exactly as given, on its own line: it shows this chat's " +
      "browser tab live, and opens the browser for the user to watch or take over.",
    "",
  ];
}

module.exports = { withBrowser, cardLines, CLOSED };
