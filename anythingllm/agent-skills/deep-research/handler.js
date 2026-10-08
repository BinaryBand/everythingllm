// Deep research: hands the question to research-runner on the host (packages/research), which
// plans it, researches it with parallel workers over SearXNG and writes a cited report.
// This file only starts the run: it answers at once with the run's live progress card
// (research.live), which the agent pastes, so the chat is free while the run goes.
//
// The run belongs to the runner: if the chat closes or AnythingLLM restarts, it still
// finishes and saves its report. research-runner can't reach AnythingLLM, so agents-runner
// is asked to follow a run from any of a workspace's chats: it puts the report in the
// workspace's documents, and tells a chat in AnythingLLM's UI that it's done
// (agents.postback). A scheduled job's run has no workspace, and only saves the file.

const hostrpc = require("../_lib/hostrpc");
const { delegatedRefusal } = require("../_lib/delegated");
const { asObject } = require("../_lib/runner");
const { scopeOf, chatOf, TOLD } = require("../_lib/scope");

const { Down } = hostrpc;

/** One request to research-runner. */
function call(op, args) {
  return hostrpc.call(hostrpc.socketPath("research", "RESEARCH_SOCKET"), op, args, { name: "the research runner" });
}

/** Ask agents-runner to keep the report in the workspace's documents and, for a chat in
 *  the UI, tell it when the run ends; whether it will. */
async function follow(self, workspace, chat, runId, card, question) {
  try {
    const socket = hostrpc.socketPath("agents", "AGENTS_SOCKET");
    const args = { run_id: runId, chat, card, question, workspace };
    await hostrpc.call(socket, "follow", args, { name: "the agents runner", timeoutMs: 10_000 });
    return true;
  } catch (e) {
    self.logger?.(`deep-research couldn't have ${runId} followed: ${e?.message || e}`);
    return false;
  }
}

/** The agent's split of the question; an empty list, which models send for an optional
 *  array they don't use, is none (the runner refuses an empty one). */
function split(subQuestions) {
  const parts = asObject(subQuestions);
  return Array.isArray(parts) && parts.length === 0 ? null : parts;
}

module.exports.runtime = {
  handler: async function ({ question, depth, sub_questions, title }) {
    const refused = delegatedRefusal(this);
    if (refused) return refused;
    const args = this.runtimeArgs || {};

    let started;
    try {
      started = await call("start", {
        question,
        depth: depth || null,
        planner: args.PLANNER_MODEL || null,
        worker: args.WORKER_MODEL || null,
        planner_fallback: args.PLANNER_FALLBACK_MODEL ?? null,
        sub_questions: split(sub_questions),
        title: title || null,
        // The chat it came from, whose app the runner tells when the run ends.
        scope: scopeOf(this),
      });
    } catch (e) {
      this.logger?.(`deep-research couldn't start a run: ${e?.message || e}`);
      if (e instanceof Down)
        return (
          `The deep research service isn't running on the server (${e.message}). Tell the user it needs ` +
          "`uv run hostctl research-setup` on the server; don't try to do the research by hand."
        );
      return `The deep research run couldn't start: ${e?.message || e}. Tell the user what went wrong; don't retry on your own.`;
    }

    const { run_id: runId, queued = 0, card = "" } = started;
    const { workspace } = scopeOf(this);
    const chat = chatOf(this);
    const followed = workspace !== "_jobs" && (await follow(this, workspace, chat, runId, card, question));
    const waits = queued ? ` It waits for ${queued} other research run${queued === 1 ? "" : "s"} to finish first.` : "";
    return [
      `Deep research started (run ${runId}). It runs on the server for several minutes and writes a cited report, ` +
        `even if the chat closes.${waits}`,
      card ? `Card: ${card}` : "",
      card
        ? "Put the Card line in your reply exactly as given, on its own line: it shows the run's progress live. " +
          "Tell the user that in a sentence."
        : "",
      followed
        ? "When it's done, the report goes into this workspace's documents under its title."
        : "When it's done, the report is saved in the agent's files, in research/.",
      followed && chat !== null ? TOLD : "",
      "Don't wait for the run, search on your own or start it again. When the user asks how it went, look for the " +
        "report in this workspace's documents, or in research/ in the agent's files.",
    ]
      .filter(Boolean)
      .join("\n\n");
  },
};

