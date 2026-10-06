// Delegate: hands a piece of work, split into tasks by the calling agent, to agents-runner on
// the host (packages/agents), which runs each task as AnythingLLM's own agent in the
// workspace of its role and answers at once with the delegation's live card. A delegated
// task can't delegate again: _lib/delegated.js refuses it.

const hostrpc = require("../_lib/hostrpc");
const { delegatedRefusal } = require("../_lib/delegated");
const { asObject } = require("../_lib/runner");

module.exports.runtime = {
  handler: async function ({ goal, tasks, then }) {
    const refused = delegatedRefusal(this);
    if (refused) return refused;
    let started;
    try {
      started = await hostrpc.call(
        hostrpc.socketPath("agents", "AGENTS_SOCKET"),
        "delegate",
        { goal: goal ?? "", tasks: asObject(tasks) ?? [], then: asObject(then) },
        { name: "the agents runner" }
      );
    } catch (e) {
      this.logger?.(`delegate: ${e?.message || e}`);
      if (e instanceof hostrpc.Down)
        return `The delegation service isn't running on the server (${e.message}). Tell the user it needs \`make agents-setup\`.`;
      if (e instanceof hostrpc.Refused) return `Error: ${e.message}`;
      return `The delegation couldn't start: ${e?.message || e}.`;
    }
    const { run_id: runId, queued = 0, card = "" } = started;
    const waits = queued ? ` It waits for ${queued} other delegation${queued === 1 ? "" : "s"} first.` : "";
    return [
      `Delegation started (run ${runId}). Its tasks run on the server for a few minutes, even if the chat closes.${waits}`,
      card ? `Card: ${card}` : "",
      card
        ? "Put the Card line in your reply exactly as given, on its own line: it shows the progress live and the results when it's done."
        : "",
      "Don't wait for it or do the tasks yourself.",
    ]
      .filter(Boolean)
      .join("\n");
  },
};
