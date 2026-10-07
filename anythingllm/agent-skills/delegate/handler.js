// Delegate: hands a piece of work, split into tasks by the calling agent, to agents-runner on
// the host (packages/agents), which runs each task as AnythingLLM's own agent in the
// workspace of its role and answers at once with the delegation's live card. A delegated
// task can't delegate again: _lib/delegated.js refuses it. A chat in AnythingLLM's UI is told
// when the delegation ends (agents.postback).

const { forward, asObject } = require("../_lib/runner");
const { chatOf, TOLD } = require("../_lib/scope");

function started({ run_id: runId, queued = 0, card = "" }, told) {
  const waits = queued ? ` It waits for ${queued} other delegation${queued === 1 ? "" : "s"} first.` : "";
  return [
    `Delegation started (run ${runId}). Its tasks run on the server for a few minutes, even if the chat closes.${waits}`,
    card ? `Card: ${card}` : "",
    card
      ? "Put the Card line in your reply exactly as given, on its own line: it shows the progress live and the results when it's done."
      : "",
    told ? TOLD : "",
    "Don't wait for it or do the tasks yourself.",
  ]
    .filter(Boolean)
    .join("\n");
}

module.exports.runtime = {
  handler: async function ({ goal, tasks, then }) {
    const chat = chatOf(this);
    return forward(this, {
      service: "agents",
      env: "AGENTS_SOCKET",
      op: "delegate",
      args: { goal: goal ?? "", tasks: asObject(tasks) ?? [], then: asObject(then), chat },
      reply: (result) => started(result, chat !== null),
    });
  },
};
