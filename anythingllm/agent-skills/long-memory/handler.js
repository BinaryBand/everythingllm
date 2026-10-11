// Long-term Memory: has memory-runner (packages/memory, memory.runner) remember, recall,
// list or forget facts beyond AnythingLLM's 25 saved memories, and ingest sandbox files for
// recall to find. The workspace and thread come from the invocation, never from the model;
// a delegated task is refused (_lib/delegated.js), and a scheduled job may only recall.

const { forward, asInteger } = require("../_lib/runner");

const ACTIONS = new Set(["recall", "remember", "list", "forget", "ingest"]);

function paths(value) {
  if (Array.isArray(value)) return value.map(String);
  return String(value ?? "")
    .split(",")
    .map((p) => p.trim())
    .filter(Boolean);
}

module.exports.runtime = {
  handler: async function ({ action, query, text, scope, id, path, k, only }) {
    const op = String(action || "recall").trim().toLowerCase();
    if (!ACTIONS.has(op)) return `Error: action is one of ${[...ACTIONS].join(", ")}.`;
    const args = {
      recall: { query, k: asInteger(k) ?? null, only: only || null },
      remember: { text, memory_scope: scope || null },
      list: {},
      forget: { id: id ?? null, path: path || null },
      ingest: { paths: paths(path) },
    }[op];
    return forward(this, {
      service: "memory",
      scoped: true,
      op,
      args,
      timeoutMs: 60_000,
      reply: (result) => result.text,
    });
  },
};
