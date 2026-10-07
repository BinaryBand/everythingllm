// What the browser skills (browse, browser-act, browser-read, browser-handoff,
// browser-login) share: the runner's socket, the call's scope (_lib/scope.js: browser-runner
// keeps a browser per workspace and a tab per chat thread by it), turning the runner's
// errors into replies, and the line each call shows in the chat while it runs, so that
// seven steps of filling a form don't all read "browser-act".

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

/** Show `line` in the chat as what the agent is doing now (AnythingLLM's introspect). */
function say(self, line) {
  try {
    self.introspect?.(line);
  } catch {}
}

/** Where browse is going, as its host alone: an address's path and query can hold a token. */
function hostOf(url) {
  const raw = String(url ?? "").trim();
  try {
    return new URL(/^[a-z][a-z0-9+.-]*:/i.test(raw) ? raw : `https://${raw}`).host || "";
  } catch {
    return "";
  }
}

const KEYS = /^(Shift\+)?(Enter|Tab|Escape|Backspace|Delete|Space|Home|End|PageUp|PageDown|Arrow(Up|Down|Left|Right))$/;

/**
 * What browser-act is doing, in a line: the element by its name from the last read (`label`,
 * the page's words) or else its ref. What's typed or chosen is never shown, and a key only
 * when it's a named one: a run of single keys could spell a password.
 */
function actLine(action, label, ref, text) {
  const name = String(label || "").replace(/\s+/g, " ").trim() || String(ref || "").trim() || "an element";
  const key = String(text ?? "").trim();
  switch (String(action ?? "")) {
    case "click": return `Clicking ${name}`;
    case "fill": return `Filling in ${name}`;
    case "type": return `Typing into ${name}`;
    case "press": return KEYS.test(key) ? `Pressing ${key}` : "Pressing a key";
    case "select": return `Choosing an option in ${name}`;
    case "check": return `Ticking ${name}`;
    case "uncheck": return `Unticking ${name}`;
    case "hover": return `Pointing at ${name}`;
    case "scroll_down": return "Scrolling down";
    case "scroll_up": return "Scrolling up";
    case "back": return "Going back";
    case "forward": return "Going forward";
    case "reload": return "Reloading the page";
    case "wait": return "Waiting for the page";
    default: return "Using the browser";
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

module.exports = { withBrowser, cardLines, say, hostOf, actLine, CLOSED };
