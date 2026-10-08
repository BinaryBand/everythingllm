// Where a skill's call came from, for the host services that keep things per workspace and
// chat thread (the sandbox, the browser): from the invocation, never from what the model
// says. A scheduled job has no workspace and gets "_jobs"; a workspace's main chat and a
// job, which have no thread, get "default", as does an API chat with none. An API or
// Telegram chat on a thread gets its thread only because anythingllm/thread-scope.js adds
// thread_id to the invocation AnythingLLM's EphemeralAgentHandler gives skills, which it
// leaves out (1.16.2); were that to stop, a workspace's API chats would share one scope.

function scopeOf(self) {
  const invocation = self.super?.handlerProps?.invocation || {};
  return {
    workspace: invocation.workspace?.slug || "_jobs",
    thread: invocation.thread_id == null ? "default" : String(invocation.thread_id),
  };
}

/** The invocation of a call from a chat in AnythingLLM's UI, or null. Only such a chat has
 *  an invocation row of its own (workspace_agent_invocations), with its uuid and thread_id
 *  (null in the workspace's main chat). An API or Telegram chat's invocation is made up for
 *  the call, with no uuid, though it has thread_id (thread-scope.js), and a scheduled job's
 *  has neither: they aren't told a job's end, nor given the chat's attachments. */
function uiInvocation(self) {
  const invocation = self.super?.handlerProps?.invocation || {};
  const row = typeof invocation.uuid === "string" && invocation.uuid !== "";
  return row && Object.hasOwn(invocation, "thread_id") ? invocation : null;
}

/** The chat a call came from, told when a job it starts ends (agents-runner's
 *  agents.postback): {workspace, thread}, the thread's id or null in the main chat; null
 *  when it isn't a chat in the UI. */
function chatOf(self) {
  const invocation = uiInvocation(self);
  const workspace = invocation?.workspace?.slug;
  if (typeof workspace !== "string" || !workspace) return null;
  const thread = invocation.thread_id;
  if (thread !== null && !Number.isInteger(thread)) return null;
  return { workspace, thread };
}

/** What a skill tells the agent when the chat will be told a job's end. */
const TOLD = "When it ends, a notice comes back into this chat (it shows once the chat is reloaded).";

module.exports = { scopeOf, chatOf, uiInvocation, TOLD };
