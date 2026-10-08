const fs = require("fs");
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const { runPreloaded, parsesAsModule } = require("./preload");

const PRELOAD = path.join(__dirname, "..", "job-guard.js");
const { patch } = require(PRELOAD);
const REAL = "/app/server/utils/agents/aibitat/plugins/create-scheduled-job/index.js";

// create-scheduled-job's plugin as AnythingLLM 1.17.0 has it, cut down.
const PLUGIN = `
const createScheduledJob = {
  name: "create-scheduled-job",
  plugin: function () {
    return {
      name: this.name,
      setup(aibitat) {
        aibitat.function({
          super: aibitat,
          name: this.name,
          handler: async function (args = {}) {
            try {
              return await this.execute(args);
            } catch (error) {
              return \`There was an error creating the scheduled job: \${error.message}\`;
            }
          },
          execute: async function (args) {
            return \`Created scheduled job "\${args.name}"\`;
          },
        });
      },
    };
  },
};

module.exports = { createScheduledJob };
`;

/** Run `node --require job-guard.js` on a plugin saved as .../create-scheduled-job/index.js,
 *  calling its handler from each of `workspaces` (null: a scheduled job, no workspace). */
function load(source, workspaces) {
  const rel = "server/utils/agents/aibitat/plugins/create-scheduled-job/index.js";
  return runPreloaded(PRELOAD, rel, source, (file) => `const { createScheduledJob } = require(${JSON.stringify(file)});
    (async () => {
      const replies = [];
      for (const slug of ${JSON.stringify(workspaces)}) {
        let tool;
        const aibitat = { handlerProps: { invocation: slug === null ? {} : { workspace: { slug } } }, function: (def) => (tool = def) };
        createScheduledJob.plugin().setup(aibitat);
        replies.push(await tool.handler.call(tool, { name: "daily" }));
      }
      console.log(JSON.stringify(replies));
    })();`);
}

test("a delegated task can't make a job; a chat and a job still can", () => {
  assert.equal(patch(PLUGIN).state, "patched");
  const { out, stderr } = load(PLUGIN, ["agents-researcher", "career", null]);
  assert.match(out[0], /^Error: this tool isn't available to a delegated task/);
  assert.deepEqual(out.slice(1), ['Created scheduled job "daily"', 'Created scheduled job "daily"']);
  assert.equal(stderr, "");
});

test("the refusal goes on the handler's own line", () => {
  const { source } = patch(PLUGIN);
  assert.equal(source.split("\n").length, PLUGIN.split("\n").length);
  assert.match(source, /handler: async function \(args = \{\}\) \{ const refused = require\(".*delegated\.js"\)\.delegatedRefusal\(this\); if \(refused\) return refused;\n/);
});

test("code that has moved is left alone and said on stderr", () => {
  const moved = PLUGIN.replace("async function (args = {})", "async function (args)");
  assert.deepEqual(patch(moved), { source: moved, state: "moved" });
  assert.equal(patch(PLUGIN + PLUGIN).state, "moved");
  const { out, stderr } = load(moved, ["agents-researcher"]);
  assert.deepEqual(out, ['Created scheduled job "daily"']);
  assert.match(stderr, /\[job-guard\] .*create-scheduled-job[\\/]index\.js isn't as expected/);
});

test("the container's own create-scheduled-job takes the patch and still parses", { skip: !fs.existsSync(REAL) && "not in the AnythingLLM container" }, () => {
  const done = patch(fs.readFileSync(REAL, "utf8"));
  assert.equal(done.state, "patched", "AnythingLLM's create-scheduled-job changed: see job-guard.js");
  parsesAsModule(done.source, REAL);
});
