// What the preload tests share (thread-scope.test.js, agent-stop.test.js): running a script
// under `node --require=<preload>` with a stand-in for the AnythingLLM file it patches, and
// checking that a patched source still parses as a module.
const os = require("os");
const fs = require("fs");
const vm = require("vm");
const assert = require("node:assert/strict");
const path = require("path");
const { spawnSync } = require("child_process");

/** Save `source` as <tmp>/<rel> and run `script(file)` under `node --require=<preload>`:
 *  its stdout as JSON, and its stderr. */
function runPreloaded(preload, rel, source, script) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "preload-"));
  const file = path.join(dir, rel);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, source);
  const run = spawnSync(process.execPath, [`--require=${preload}`, "-e", script(file)], { encoding: "utf8", env: { ...process.env, NODE_OPTIONS: "" } });
  fs.rmSync(dir, { recursive: true, force: true });
  assert.equal(run.status, 0, run.stderr);
  return { out: JSON.parse(run.stdout), stderr: run.stderr };
}

/** Throws unless `source` parses as the CommonJS module `filename`. */
function parsesAsModule(source, filename) {
  new vm.Script(`(function (exports, require, module, __filename, __dirname) {${source}\n})`, { filename });
}

module.exports = { runPreloaded, parsesAsModule };
