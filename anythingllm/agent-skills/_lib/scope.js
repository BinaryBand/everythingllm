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

module.exports = { scopeOf };
