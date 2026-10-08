// What the sandbox skills (run-code, write-file, publish, build-site) share: the runner's
// socket, the scope of a call, turning the runner's errors into replies for the agent, and
// telling it which of its pages in /public a call changed, what in them the pages site's
// CSP blocks, and the notices about the sandbox their scripts run in.
//
// The scope is where the call came from (_lib/scope.js): sandbox-runner (packages/sandbox)
// mounts /work, /project, /shared/<workspace> and /public by it.

const { withRunner } = require("./runner");

/**
 * Run `work(request)` for a skill, where request(op, args) calls the runner with the call's
 * scope added (_lib/runner.js's withRunner). `closed` is the reply when the chat closes first.
 */
function withSandbox(self, work, { closed } = {}) {
  return withRunner(self, { service: "sandbox", label: "The sandbox", scoped: true, timeoutMs: 60_000, closed }, work);
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
