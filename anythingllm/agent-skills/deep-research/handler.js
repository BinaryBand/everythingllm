// Deep research: hands the question to research-runner on the host (packages/research), which
// plans it, researches it with parallel workers over SearXNG and publishes a cited report
// to the `research` Zola site. This file only starts the run: it answers at once with the
// run's live progress card (research.live), which the agent pastes, so the chat is free
// while the run goes. The card links to the report once it's published.
//
// The run belongs to the runner: if the chat closes or AnythingLLM restarts, it still
// finishes and publishes. research-runner can't reach AnythingLLM, so for a chat in
// AnythingLLM's UI, agents-runner is asked to follow the run and tell the chat when it ends
// (agents.postback).

const hostrpc = require("../_lib/hostrpc");
const { delegatedRefusal } = require("../_lib/delegated");
const { asObject } = require("../_lib/runner");
const { scopeOf, chatOf, TOLD } = require("../_lib/scope");

const { Down } = hostrpc;

/** One request to research-runner. */
function call(op, args) {
  return hostrpc.call(hostrpc.socketPath("research", "RESEARCH_SOCKET"), op, args, { name: "the research runner" });
}

/** Ask agents-runner to tell the chat when the run ends; whether it will. */
async function follow(self, chat, runId, card, question) {
  try {
    const socket = hostrpc.socketPath("agents", "AGENTS_SOCKET");
    const args = { run_id: runId, chat, card, question };
    await hostrpc.call(socket, "follow", args, { name: "the agents runner", timeoutMs: 10_000 });
    return true;
  } catch (e) {
    self.logger?.(`deep-research couldn't have ${runId} followed: ${e?.message || e}`);
    return false;
  }
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
        site: args.SITE || null,
        sub_questions: asObject(sub_questions),
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
    const chat = chatOf(this);
    const told = chat !== null && (await follow(this, chat, runId, card, question));
    const waits = queued ? ` It waits for ${queued} other research run${queued === 1 ? "" : "s"} to finish first.` : "";
    return [
      `Deep research started (run ${runId}). It runs on the server for several minutes and publishes a cited report ` +
        `to the research site, even if the chat closes.${waits}`,
      card ? `Card: ${card}` : "",
      card
        ? "Put the Card line in your reply exactly as given, on its own line: it shows the run's progress live and " +
          "opens the report once it's published. Tell the user that in a sentence."
        : "Tell the user the report will be on the research site when it's done.",
      told ? TOLD : "",
      "Don't wait for the run, search on your own or start it again. When the user asks how it went, find the " +
        "report with `sites list_entries`.",
    ]
      .filter(Boolean)
      .join("\n\n");
  },
};

