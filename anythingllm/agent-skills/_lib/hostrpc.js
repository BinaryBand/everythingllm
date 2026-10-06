// The node side of packages/hostrpc, for skills that front a host service: one request per
// connection over a Unix socket, a line of JSON each way ({op, args} -> {ok, result|error}).
// This folder has no plugin.json, so AnythingLLM doesn't load it as a skill; skills
// require it as "../_lib/hostrpc".

const net = require("net");
const path = require("path");

/** A service's socket: `env` if set, else storage/<folder>/runner.sock as the container sees it. */
function socketPath(folder, env) {
  return process.env[env] || path.join(process.env.STORAGE_DIR || "/app/server/storage", folder, "runner.sock");
}

/** The service isn't there: no socket, or nobody listening on it. */
class Down extends Error {}
/** The service answered with an error. */
class Refused extends Error {}

/**
 * One request to the service on `socket`. Resolves with its result. An abort of `signal`
 * closes the connection and resolves with null.
 */
function call(socket, op, args, { name, signal = null, timeoutMs = 60_000 }) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return resolve(null);
    const conn = net.createConnection(socket);
    conn.setEncoding("utf8"); // a character split across chunks stays whole
    let buffer = "";
    let settled = false;
    const finish = (fn, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      conn.destroy();
      fn(value);
    };
    const onAbort = () => finish(resolve, null);
    const timer = setTimeout(() => finish(reject, new Error(`${name} didn't answer within ${timeoutMs / 1000} s`)), timeoutMs);
    signal?.addEventListener("abort", onAbort, { once: true });
    conn.on("connect", () => conn.write(JSON.stringify({ op, args }) + "\n"));
    conn.on("data", (chunk) => {
      buffer += chunk;
      const end = buffer.indexOf("\n");
      if (end === -1) return;
      let reply;
      try {
        reply = JSON.parse(buffer.slice(0, end));
      } catch {
        return finish(reject, new Error(`${name}'s answer wasn't JSON`));
      }
      if (reply.ok) finish(resolve, reply.result);
      else finish(reject, new Refused(reply.error || "unknown error"));
    });
    conn.on("error", (e) =>
      finish(reject, ["ENOENT", "ECONNREFUSED"].includes(e.code) ? new Down(`${e.code} on ${socket}`) : e)
    );
    conn.on("end", () => finish(reject, new Error(`${name} closed the connection without answering`)));
  });
}

module.exports = { call, socketPath, Down, Refused };
