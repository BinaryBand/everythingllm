// What a skill that fronts a host service needs: refuse a delegated task, send ops their args
// over the service's socket, and return the text they come to. Never throws (a skill that
// throws ends the chat): a failure becomes the reply.

const { call, socketPath, Closed, Down, Refused } = require("./hostrpc");
const { delegatedRefusal } = require("./delegated");
const { scopeOf } = require("./scope");

/**
 * Run `work(request)` for a skill, where request(op, args) calls the runner of `service` (its
 * folder in storage and its unit's name: agents -> agents-runner; <SERVICE>_SOCKET can point
 * at another socket) and, when `scoped`, adds the call's scope ({workspace, thread}, from the
 * invocation, never the model). `label` names the service in a failure, and `closed` is the
 * reply when the chat closes first.
 */
async function withRunner(
  self,
  { service, label = `The ${service} service`, scoped = false, timeoutMs = 120_000, closed = "The chat closed." },
  work
) {
  const refused = delegatedRefusal(self);
  if (refused) return refused;
  const signal = self.super?.abortController?.signal ?? null;
  const scope = scoped ? { scope: scopeOf(self) } : {};
  const request = (op, args) =>
    call(socketPath(service, `${service.toUpperCase()}_SOCKET`), op, { ...args, ...scope }, { name: `the ${service} runner`, signal, timeoutMs });
  try {
    return await work(request);
  } catch (e) {
    if (e instanceof Closed) return closed;
    self.logger?.(`${service}: ${e?.message || e}`);
    if (e instanceof Down)
      return `${label} isn't running on the server (${e.message}). Tell the user it needs \`uv run hostctl ${service}-setup\`.`;
    if (e instanceof Refused) return `Error: ${e.message}`;
    return `${label} failed: ${e?.message || e}`;
  }
}

/** One op of `service`, its result turned into the reply by `reply` (by default the result
 *  itself, or its JSON). */
async function forward(self, { op, args, reply = asText, ...spec }) {
  return withRunner(self, spec, async (request) => reply(await request(op, args)));
}

/** The lines that hand the agent a live card for its reply, `what` saying what it shows;
 *  none without one. */
function cardLines(card, what) {
  return card ? [`Card: ${card}`, `Put the Card line in your reply exactly as given, on its own line: ${what}`] : [];
}

/** A number a model sometimes sends as text: digits become a number, anything else (e.g. "all") stays. */
function asInteger(value) {
  return typeof value === "string" && /^\d+$/.test(value.trim()) ? Number(value) : value;
}

function asText(result) {
  return typeof result === "string" ? result : JSON.stringify(result);
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

module.exports = { withRunner, forward, cardLines, asObject, asFlag, asInteger };
