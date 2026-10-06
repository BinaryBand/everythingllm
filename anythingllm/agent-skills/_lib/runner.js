// What a skill that fronts one op of a host service needs (packages/<service>/tools.py, whose
// SKILLS name the ops that are skills rather than MCP tools): refuse a delegated task, send
// the op its args over the service's socket, and return the text it answers with. Never
// throws (a skill that throws ends the chat): a failure becomes the reply.

const { call, socketPath, Down, Refused } = require("./hostrpc");
const { delegatedRefusal } = require("./delegated");

/**
 * `service` is the runner's folder in storage and its unit's name (sites -> sites-runner),
 * `env` the variable that can point at another socket.
 */
async function forward(self, { service, env, op, args, timeoutMs = 120_000 }) {
  const refused = delegatedRefusal(self);
  if (refused) return refused;
  const signal = self.super?.abortController?.signal ?? null;
  try {
    const result = await call(socketPath(service, env), op, args, { name: `the ${service} runner`, signal, timeoutMs });
    if (result === null) return "The chat closed.";
    return typeof result === "string" ? result : JSON.stringify(result);
  } catch (e) {
    self.logger?.(`${op}: ${e?.message || e}`);
    if (e instanceof Down)
      return `The ${service} service isn't running on the server (${e.message}). Tell the user it needs \`make ${service}-setup\`.`;
    if (e instanceof Refused) return `Error: ${e.message}`;
    return `${op} failed: ${e?.message || e}`;
  }
}

/** An object argument, which a model sometimes sends as JSON text; null when there's none. */
function asObject(value) {
  if (value == null || value === "") return null;
  if (typeof value === "string") {
    try {
      return JSON.parse(value);
    } catch {
      return value; // the runner says what's wrong with it
    }
  }
  return value;
}

/** A true/false argument, which a model sometimes sends as text; null when it's left out. */
function asFlag(value) {
  if (value == null || value === "") return null;
  return value === true || String(value).toLowerCase() === "true";
}

module.exports = { forward, asObject, asFlag };
