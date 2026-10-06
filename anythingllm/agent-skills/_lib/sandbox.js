// What the sandbox skills (run-code, write-file, publish, build-site) share: the runner's
// socket, the scope of a call, turning the runner's errors into replies for the agent, and
// telling it which of its pages in /public a call changed.
//
// The scope is where the call came from, never what the model says: the workspace (a
// scheduled job has none, and gets "_jobs") and the chat thread ("default" for a
// workspace's main chat, and for API, Telegram and job runs, which carry no thread).
// sandbox-runner (packages/sandbox) mounts /work, /project, /shared/<workspace> and /public
// by it.

const { call, socketPath, Down, Refused } = require("./hostrpc");

/**
 * Run `work(request)` for a skill, where request(op, args) calls the runner with the
 * call's scope added. Never throws (a skill that throws ends the chat): a failure
 * becomes the reply, and a closed chat resolves request() with null.
 */
async function withSandbox(self, work) {
  const signal = self.super?.abortController?.signal ?? null;
  const invocation = self.super?.handlerProps?.invocation || {};
  const scope = {
    workspace: invocation.workspace?.slug || "_jobs",
    thread: invocation.thread_id == null ? "default" : String(invocation.thread_id),
  };
  const request = (op, args) =>
    call(socketPath("sandbox", "SANDBOX_SOCKET"), op, { scope, ...args }, { name: "the sandbox runner", signal });
  try {
    return await work(request);
  } catch (e) {
    self.logger?.(`sandbox: ${e?.message || e}`);
    if (e instanceof Down)
      return `The sandbox isn't running on the server (${e.message}). Tell the user it needs \`make sandbox-setup\`.`;
    if (e instanceof Refused) return `Error: ${e.message}`;
    return `The sandbox failed: ${e?.message || e}`;
  }
}

/** The lines saying which pages in /public a run, write or build changed (its `published`). */
function publishedLines(p) {
  if (!p) return [];
  const lines = [];
  for (const page of p.live || []) {
    lines.push(`live: ${page.url}`);
    if (page.blocked?.length)
      lines.push(`warning: the pages site blocks ${page.blocked.join(", ")} in ${page.slug}; it will show without them`);
  }
  for (const slug of p.removed || []) lines.push(`gone: /public/${slug}`);
  return lines;
}

module.exports = { withSandbox, publishedLines };
