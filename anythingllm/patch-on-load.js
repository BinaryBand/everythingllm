// What the preloads that patch AnythingLLM's own files as they load share (thread-scope.js,
// agent-stop.js, job-guard.js; NODE_OPTIONS in host/quadlet/anythingllm.container.in): one hook on
// Module.prototype._compile that hands each target's source to its patch, and the
// `node <preload> --check` that prints a patch's state for `uv run hostctl health`. A patch
// is `patch(source)` -> {source, state}, state "patched", "upstream" (AnythingLLM does it
// itself; left alone) or "moved" (the code isn't as expected; left alone and said on stderr).
const fs = require("fs");
const Module = require("module");

const patches = []; // {target, patch, tag, moved}, the preloads loaded

/** Patch `file` (matched by `target` as it loads) with `patch` in a preloaded process, or
 *  print its state when `mod` (the preload's module) is run with --check. `tag` and `moved`
 *  (what goes wrong without it) make the stderr line for code that has moved. */
function patchOnLoad(mod, { file, target, patch, tag, moved }) {
  if (require.main === mod) {
    if (process.argv.includes("--check")) console.log(patch(fs.readFileSync(file, "utf8")).state);
    return;
  }
  if (require.main) return; // required by a test or a script, not preloaded
  // Preloaded (no main module yet). A fork (Bree's scheduled jobs) inherits execArgv.
  const self = `--require=${mod.filename}`;
  if (!process.execArgv.includes(self)) process.execArgv.push(self);
  if (patches.length === 0) hook();
  patches.push({ target, patch, tag, moved });
}

function hook() {
  const compile = Module.prototype._compile;
  Module.prototype._compile = function (content, filename, ...rest) {
    if (typeof content === "string")
      for (const p of patches) {
        if (!p.target.test(filename)) continue;
        const done = p.patch(content);
        if (done.state === "moved") process.stderr.write(`[${p.tag}] ${filename} isn't as expected; ${p.moved}\n`);
        content = done.source;
      }
    return compile.call(this, content, filename, ...rest);
  };
}

module.exports = { patchOnLoad };
