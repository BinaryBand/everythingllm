// Deep research: hands the question to research-runner on the host (src/mcps/research), which
// plans it, researches it with parallel workers over SearXNG and publishes a cited report
// to the `research` Zola site. This file shows its progress while the chat is open and
// replies with what the runner says to tell the user.
//
// The run belongs to the runner: if the chat closes or AnythingLLM restarts, it still
// finishes, publishes and adds the report to the workspace.

const hostrpc = require("../_lib/hostrpc");

const { Down, Refused } = hostrpc;
// Tries at a wait before giving up on the runner, a few seconds apart.
const WAIT_TRIES = 3;
const RETRY_MS = 3_000;
const OFF = /^(no|off|false|0)$/i;

/**
 * One request to research-runner; an abort of `signal` resolves with null. A wait is a
 * long poll of up to 45 s, inside hostrpc.call's 60 s.
 */
function call(op, args, { signal = null } = {}) {
  return hostrpc.call(hostrpc.socketPath("research", "RESEARCH_SOCKET"), op, args, { name: "the research runner", signal });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

module.exports.runtime = {
  handler: async function ({ question, depth }) {
    const args = this.runtimeArgs || {};
    const workspace = this.super?.handlerProps?.invocation?.workspace;
    // The session aborts whenever the chat's websocket closes: the Stop button, but also
    // a closed tab, a thread switch or a sleeping phone. The run carries on regardless.
    const session = this.super?.abortController?.signal ?? null;

    let started;
    try {
      started = await call("start", {
        question,
        depth: depth || null,
        planner: args.PLANNER_MODEL || null,
        worker: args.WORKER_MODEL || null,
        planner_fallback: args.PLANNER_FALLBACK_MODEL ?? null,
        site: args.SITE || null,
        embed: !OFF.test(String(args.EMBED_IN_WORKSPACE ?? "").trim()),
        workspace: workspace?.slug || null,
        workspace_name: workspace?.name || null,
      });
    } catch (e) {
      this.logger?.(`deep-research couldn't start a run: ${e?.message || e}`);
      if (e instanceof Down)
        return (
          `The deep research service isn't running on the server (${e.message}). Tell the user it needs ` +
          "`make research-setup` on the server; don't try to do the research by hand."
        );
      return `The deep research run couldn't start: ${e?.message || e}. Tell the user what went wrong; don't retry on your own.`;
    }

    const runId = started.run_id;
    let since = 0;
    let failures = 0;
    for (;;) {
      if (session?.aborted) return "The chat closed; the research carries on and is published to the research site when it's done.";
      let news;
      try {
        news = await call("wait", { run_id: runId, since }, { signal: session });
        failures = 0;
      } catch (e) {
        // A run the runner doesn't know (it restarted) won't come back; anything else may be a blip.
        if (e instanceof Refused || ++failures >= WAIT_TRIES) {
          this.logger?.(`deep-research lost run ${runId}: ${e?.message || e}`);
          return (
            `Lost touch with the deep research run (${e?.message || e}). It may still finish and be published ` +
            "to the research site; tell the user to check there, or to ask about it later (audit research_run). " +
            "Don't start it again on your own."
          );
        }
        await sleep(RETRY_MS);
        continue;
      }
      if (news === null) continue; // aborted mid-wait; the top of the loop says so
      for (const line of news.events) this.introspect(`Deep research: ${line}`);
      since += news.events.length;
      if (news.done) {
        const { reply, sources = [] } = news.result || {};
        if (sources.length)
          this.super?.addCitation?.(
            sources.map((s) => ({ id: s.url, title: s.title, text: "", chunkSource: `link://${s.url}`, score: null }))
          );
        return reply || "The deep research run ended without a reply. Tell the user to check the research site.";
      }
    }
  },
};

