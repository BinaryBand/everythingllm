const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

const { asObject, asFlag, asInteger } = require("../runner");

const SKILLS = path.join(__dirname, "..", "..");
// Skills that only read, and so may run in a delegated task. None yet: every skill of ours
// writes, acts or delegates.
const READS = new Set([]);

// A fake host service on its own socket: answers each request with respond(op, args).
async function fakeService(respond) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "svc-sock-"));
  const socket = path.join(dir, "runner.sock");
  const requests = [];
  const server = net.createServer((conn) => {
    let buffer = "";
    conn.on("data", (chunk) => {
      buffer += chunk;
      if (!buffer.includes("\n")) return;
      const msg = JSON.parse(buffer.split("\n")[0]);
      requests.push(msg);
      conn.end(JSON.stringify(respond(msg.op, msg.args)) + "\n");
    });
  });
  await new Promise((r) => server.listen(socket, r));
  return { socket, requests, close: () => new Promise((r) => server.close(r)) };
}

function agent(workspace) {
  return { logger: () => {}, introspect: () => {}, super: { handlerProps: { invocation: { workspace: { slug: workspace }, thread_id: 3 } } } };
}

test("every skill that writes, acts or delegates refuses a delegated task, before reaching any service", async () => {
  const service = await fakeService(() => ({ ok: true, result: "done" }));
  const envs = ["SANDBOX_SOCKET", "RESEARCH_SOCKET", "SITES_SOCKET", "PODCASTS_SOCKET", "AUDIT_SOCKET", "AGENTS_SOCKET", "BROWSER_SOCKET"];
  for (const env of envs) process.env[env] = service.socket;
  try {
    const skills = fs.readdirSync(SKILLS).filter((d) => fs.existsSync(path.join(SKILLS, d, "plugin.json")));
    assert.ok(skills.includes("write-entry") && skills.includes("run-code"));
    for (const skill of skills) {
      if (READS.has(skill)) continue;
      const { handler } = require(path.join(SKILLS, skill, "handler.js")).runtime;
      const reply = await handler.call(agent("agents-worker"), {});
      assert.match(reply, /^Error: this tool isn't available to a delegated task/, skill);
    }
    assert.deepEqual(service.requests, []);
  } finally {
    for (const env of envs) delete process.env[env];
    await service.close();
  }
});

test("forward sends the op and its args to the service and gives back its text", async () => {
  const service = await fakeService((op, args) =>
    args.slug === "nope" ? { ok: false, error: "there's no entry 'nope'" } : { ok: true, result: `${op} ok` }
  );
  process.env.SITES_SOCKET = service.socket;
  try {
    const writeEntry = require("../../write-entry/handler").runtime;
    const reply = await writeEntry.handler.call(agent("career"), {
      site: "news", section: "editions", slug: "2026-10-06", title: "T", date: "2026-10-06",
      extra: '{"lede": "x"}', overwrite: "true",
    });
    assert.equal(reply, "write_entry ok");
    assert.deepEqual(service.requests[0], {
      op: "write_entry",
      args: { site: "news", section: "editions", slug: "2026-10-06", title: "T", date: "2026-10-06", extra: { lede: "x" }, overwrite: true },
    });
    const deleteEntry = require("../../delete-entry/handler").runtime;
    assert.equal(await deleteEntry.handler.call(agent("career"), { site: "news", section: "editions", slug: "nope" }), "Error: there's no entry 'nope'");
    // Scheduled jobs have no workspace, and go ahead.
    const job = { logger: () => {}, super: { handlerProps: { invocation: {} } } };
    assert.equal(await deleteEntry.handler.call(job, { site: "news", section: "editions", slug: "x" }), "delete_entry ok");
  } finally {
    delete process.env.SITES_SOCKET;
    await service.close();
  }
  process.env.PODCASTS_SOCKET = "/nonexistent/podcasts.sock";
  try {
    const addPodcast = require("../../add-podcast/handler").runtime;
    assert.match(await addPodcast.handler.call(agent("career"), { url: "https://x/feed" }), /podcasts service isn't running.*uv run hostctl podcasts-setup/);
  } finally {
    delete process.env.PODCASTS_SOCKET;
  }
});

test("a generated skill sends what's set, coerced, and leaves the rest to the op's defaults", async () => {
  const service = await fakeService(() => ({ ok: true, result: "added" }));
  process.env.PODCASTS_SOCKET = service.socket;
  try {
    const addPodcast = require("../../add-podcast/handler").runtime;
    await addPodcast.handler.call(agent("career"), { url: "u", keep: "12" });
    await addPodcast.handler.call(agent("career"), { url: "u", keep: "all", scrub_ads: false, transcribe: "true" });
    await addPodcast.handler.call(agent("career"), { url: "u", keep: null, ad_words: "", rules: "", other: 1 });
    assert.deepEqual(service.requests.map((r) => r.args), [
      { url: "u", keep: 12 },
      { url: "u", keep: "all", scrub_ads: false, transcribe: true },
      { url: "u", rules: "" }, // "" is a setting (every episode); an enum's "" isn't
    ]);
  } finally {
    delete process.env.PODCASTS_SOCKET;
    await service.close();
  }
});

test("arguments a model sends as text", () => {
  assert.deepEqual(asObject('{"1": "fix it"}'), { 1: "fix it" });
  assert.equal(asObject(""), null);
  assert.equal(asObject("not json"), "not json");
  assert.equal(asFlag("true"), true);
  assert.equal(asFlag("False"), false);
  assert.equal(asFlag(undefined), null);
  assert.equal(asInteger("7"), 7);
  assert.equal(asInteger("all"), "all");
  assert.equal(asInteger(3), 3);
});

test("delegate starts a delegation and hands back its card", async () => {
  const service = await fakeService((op, args) =>
    op === "delegate" ? { ok: true, result: { run_id: "dg-1", queued: 0, card: "[![D](c.png)](p)" } } : { ok: false, error: "?" }
  );
  process.env.AGENTS_SOCKET = service.socket;
  try {
    const delegate = require("../../delegate/handler").runtime;
    const reply = await delegate.handler.call(agent("career"), {
      goal: "compare",
      tasks: '[{"name": "a", "profile": "worker", "instructions": "x"}]',
    });
    assert.match(reply, /Delegation started \(run dg-1\)[\s\S]*Card: \[!\[D\]\(c\.png\)\]\(p\)/);
    assert.deepEqual(service.requests[0], {
      op: "delegate",
      args: { goal: "compare", tasks: [{ name: "a", profile: "worker", instructions: "x" }], then: null },
    });
  } finally {
    delete process.env.AGENTS_SOCKET;
    await service.close();
  }
});
