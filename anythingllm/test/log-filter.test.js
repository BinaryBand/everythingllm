const os = require("os");
const fs = require("fs");
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const { execFileSync } = require("child_process");

const FILTER = path.join(__dirname, "..", "log-filter.js");
const { filter } = require(FILTER);

test("a transport message keeps its prefix and loses its payload", () => {
  const line = `[MCPHypervisor] sites - Transport message: {"jsonrpc":"2.0","id":2,"result":{"secret":"x"}}\n`;
  assert.equal(filter(line), "[MCPHypervisor] sites - Transport message: [48 chars omitted]\n");
});

test("a tool result logged across several lines is dropped to the end of the chunk", () => {
  const chunk =
    "[EphemeralAgentHandler] MCP server: sites:list_entries completed successfully {\n" +
    "  content: [ { type: 'text', text: 'page text the agent read' } ],\n" +
    "  isError: false\n" +
    "}\n";
  const out = filter(chunk);
  assert.match(out, /^\[EphemeralAgentHandler\] MCP server: sites:list_entries completed successfully \[\d+ chars omitted\]\n$/);
  assert.doesNotMatch(out, /page text/);
});

test("a failed tool call keeps the start of its payload", () => {
  const json = `{"content":[{"type":"text","text":"no site named 'x'"}],"isError":true,"pad":"${"y".repeat(500)}"}`;
  const out = filter(`[MCPHypervisor] sites - Transport message: ${json}\n`);
  assert.ok(out.includes(json.slice(0, 300)));
  assert.ok(out.endsWith(` [${json.length - 300} chars omitted]\n`));
  const dump = "[AgentHandler] MCP server: sites:write_entry completed successfully {\n  content: [ [Object] ],\n  isError: true\n}";
  assert.equal(filter(dump), dump);
});

test("other lines pass, cut at 2000 characters", () => {
  assert.equal(filter("[backend] info: started\n"), "[backend] info: started\n");
  const long = filter(`${"a".repeat(2500)}\n`);
  assert.equal(long, `${"a".repeat(2000)} [501 chars omitted]\n`);
});

// Runs `main` from a temp folder holding `files`, with the filter preloaded; its stdout.
function preloaded(files, main) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "log-filter-"));
  for (const [name, code] of Object.entries(files)) fs.writeFileSync(path.join(dir, name), code);
  return execFileSync(process.execPath, [path.join(dir, main)], {
    env: { ...process.env, NODE_OPTIONS: `--require ${FILTER}` },
    encoding: "utf8",
  });
}

test("preloaded, it filters Buffer writes, in forked workers too", () => {
  const out = preloaded(
    {
      "main.js": [
        `process.stdout.write(Buffer.from("[MCPHypervisor] a - Transport message: {\\n  secret: 1\\n}\\n"));`,
        `process.stdout.write(new Uint8Array(Buffer.from("plain\\n")));`,
        // Like Bree's scheduled-job workers: a fork writing to the same stdout itself.
        `require("child_process").fork(__dirname + "/worker.js").on("exit", () => {});`,
      ].join("\n"),
      "worker.js": `process.stdout.write("[EphemeralAgentHandler] MCP server: podcasts:refresh_podcasts completed successfully {\\n  text: 'Downloading'\\n}\\n");`,
    },
    "main.js"
  );
  assert.equal(
    out,
    "[MCPHypervisor] a - Transport message: [15 chars omitted]\nplain\n" +
      "[EphemeralAgentHandler] MCP server: podcasts:refresh_podcasts completed successfully [25 chars omitted]\n"
  );
});

test("preloaded, it stays out of spawned processes (MCP servers)", () => {
  // The child reports on itself: its output then passes the parent's filter unchanged.
  const out = preloaded(
    {
      "main.js": `process.stdout.write(require("child_process").execFileSync(process.execPath, [__dirname + "/server.js"]));`,
      "server.js": [
        `const patched = process.stdout.write.toString().includes("filter(chunk)");`,
        `process.stdout.write("patched=" + patched + " NODE_OPTIONS=" + (process.env.NODE_OPTIONS ?? "unset") + "\\n");`,
      ].join("\n"),
    },
    "main.js"
  );
  assert.equal(out, "patched=false NODE_OPTIONS=unset\n");
});

test("required by a test, it doesn't patch stdout", () => {
  assert.notEqual(process.stdout.write.toString().includes("filter(chunk)"), true);
});
