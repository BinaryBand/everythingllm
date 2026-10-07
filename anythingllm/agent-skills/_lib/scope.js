// Where a skill's call came from, for the host services that keep things per workspace and
// chat thread (the sandbox, the browser): from the invocation, never from what the model
// says. A scheduled job has no workspace and gets "_jobs"; a workspace's main chat, and
// API, Telegram and job runs, which carry no thread, get "default".

function scopeOf(self) {
  const invocation = self.super?.handlerProps?.invocation || {};
  return {
    workspace: invocation.workspace?.slug || "_jobs",
    thread: invocation.thread_id == null ? "default" : String(invocation.thread_id),
  };
}

/** The chat in AnythingLLM's UI a call came from, told when a job it starts ends
 *  (agents-runner's agents.postback): {workspace, thread}, the thread's id, or null in the
 *  workspace's main chat. Only a chat in the UI has an invocation row of its own, with
 *  thread_id; API, Telegram and scheduled job runs have none, and get null. */
function chatOf(self) {
  const invocation = self.super?.handlerProps?.invocation || {};
  const workspace = invocation.workspace?.slug;
  if (typeof workspace !== "string" || !workspace || !Object.hasOwn(invocation, "thread_id")) return null;
  const thread = invocation.thread_id;
  if (thread !== null && !Number.isInteger(thread)) return null;
  return { workspace, thread };
}

module.exports = { scopeOf, chatOf };
