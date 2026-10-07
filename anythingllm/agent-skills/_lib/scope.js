// Where a skill's call came from, for the host services that keep things per workspace and
// chat thread (the sandbox, the browser): from the invocation, never from what the model
// says. A scheduled job has no workspace and gets "_jobs"; a workspace's main chat and a
// job, which have no thread, get "default". So does every API and Telegram chat, thread or
// not: AnythingLLM (1.16.2) holds their thread but leaves thread_id out of the invocation
// its EphemeralAgentHandler gives skills, so a workspace's API chats share one scope until
// it passes it.

function scopeOf(self) {
  const invocation = self.super?.handlerProps?.invocation || {};
  return {
    workspace: invocation.workspace?.slug || "_jobs",
    thread: invocation.thread_id == null ? "default" : String(invocation.thread_id),
  };
}

/** The invocation of a call from a chat in AnythingLLM's UI, or null. Only such a chat has
 *  an invocation row of its own, with thread_id (null in the workspace's main chat). API and
 *  Telegram runs have no such key, because AnythingLLM (1.16.2) leaves their thread out of
 *  the invocation it gives skills, not because they have none; scheduled jobs have none.
 *  Were it to pass an API chat's thread_id, that chat would count as one here too. */
function uiInvocation(self) {
  const invocation = self.super?.handlerProps?.invocation || {};
  return Object.hasOwn(invocation, "thread_id") ? invocation : null;
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
