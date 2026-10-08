// Keeping delegated tasks away from what writes, acts or delegates (docs/.proposals/agents.md):
// agents-runner runs each task as AnythingLLM's own agent in an `agents-*` workspace, and
// every tool loads there, so each of our skills that writes, acts or delegates refuses
// when its call comes from one. A skill call carries its workspace; the model can't choose it.
// Everywhere else (chats, Nilson's API chats, scheduled jobs) nothing changes.

const PREFIX = "agents-";

/** The reply that refuses a delegated task, or null when the call may go ahead. */
function delegatedRefusal(self) {
  const workspace = self?.super?.handlerProps?.invocation?.workspace?.slug || "";
  if (!workspace.startsWith(PREFIX)) return null;
  return (
    "Error: this tool isn't available to a delegated task. Delegated tasks only read and " +
    "report back; say in your reply what should be written or done, and the agent that " +
    "delegated the task will decide."
  );
}

module.exports = { delegatedRefusal };
