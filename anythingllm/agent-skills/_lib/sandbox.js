// What the sandbox skills (run-code, write-file, publish, build-site) share: the runner's
// socket, the scope of a call, turning the runner's errors into replies for the agent, and
// telling it which of its pages in /public a call changed, what in them the pages site's
// CSP blocks, and the notices about the sandbox their scripts run in.
//
// The scope is where the call came from (_lib/scope.js): sandbox-runner (packages/sandbox)
// mounts /work, /project, /shared/<workspace> and /public by it.

const { call, socketPath, Down, Refused } = require("./hostrpc");
const { delegatedRefusal } = require("./delegated");
const { scopeOf } = require("./scope");

/**
 * Run `work(request)` for a skill, where request(op, args) calls the runner with the
 * call's scope added. Never throws (a skill that throws ends the chat): a failure
 * becomes the reply, and a closed chat resolves request() with null.
 */
async function withSandbox(self, work) {
  const refused = delegatedRefusal(self);
  if (refused) return refused;
  const signal = self.super?.abortController?.signal ?? null;
  const scope = scopeOf(self);
  const request = (op, args) =>
    call(socketPath("sandbox", "SANDBOX_SOCKET"), op, { scope, ...args }, { name: "the sandbox runner", signal });
  try {
    return await work(request);
  } catch (e) {
    self.logger?.(`sandbox: ${e?.message || e}`);
    if (e instanceof Down)
      return `The sandbox isn't running on the server (${e.message}). Tell the user it needs \`uv run hostctl sandbox-setup\`.`;
    if (e instanceof Refused) return `Error: ${e.message}`;
    return `The sandbox failed: ${e?.message || e}`;
  }
}

/**
 * The lines about one page: what the pages site's CSP blocks in it (`blocked`), and its
 * `notices` (that it has scripts, and what of the sandbox they run in it runs into).
 * `slug` names the page when the reply is about more than one.
 */
function pageLines(page, slug) {
  const lines = [];
  const where = slug ? ` in ${slug}` : "";
  if (page.blocked?.length)
    lines.push(`warning: the pages site blocks ${page.blocked.join(", ")}${where}; it will show without them`);
  for (const notice of page.notices || []) lines.push(`note${slug ? ` (${slug})` : ""}: ${notice}`);
  return lines;
}

/** The lines saying which pages in /public a run, write or build changed (its `published`). */
function publishedLines(p) {
  if (!p) return [];
  const lines = [];
  for (const page of p.live || []) lines.push(`live: ${page.url}`, ...pageLines(page, page.slug));
  for (const slug of p.removed || []) lines.push(`gone: /public/${slug}`);
  return lines;
}

module.exports = { withSandbox, publishedLines, pageLines };
