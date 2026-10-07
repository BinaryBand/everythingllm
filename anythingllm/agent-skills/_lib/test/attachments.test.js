const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const os = require("os");
const path = require("path");

const attachments = require("../attachments");
const { fakeService } = require("./fakeservice");
const runCode = require("../../run-code/handler").runtime;

// A fake sandbox-runner that answers every request with `result`.
async function fakeRunner(result) {
  const service = await fakeService(() => ({ ok: true, result }));
  process.env.SANDBOX_SOCKET = service.socket;
  return service;
}

// AnythingLLM's Prisma client, as far as the lookup uses it.
function fakePrisma(rows, { fail = false } = {}) {
  const queries = [];
  return {
    queries,
    workspace_parsed_files: {
      findMany: async (query) => {
        queries.push(query);
        if (fail) throw new Error("database is locked");
        return rows.slice(0, query.take);
      },
    },
  };
}

function row(title, file) {
  return { metadata: JSON.stringify({ title, location: `direct-uploads/${file}`, wordCount: 3 }) };
}

// A chat in AnythingLLM's UI: its invocation is a database row, thread_id and user_id included.
function chat(invocation = { workspace: { id: 7, slug: "career" }, thread_id: 12, user_id: 3 }) {
  const logs = [];
  return {
    logs,
    self: { introspect: () => {}, logger: (m) => logs.push(m), super: { handlerProps: { invocation } } },
  };
}

const done = {
  exit_code: 0, timed_out: false, oom_killed: false, timeout: 60, seconds: 0.4,
  stdout: "3\n", stderr: "", changed: [], changed_more: 0,
};

async function withPrisma(prisma, fn) {
  const load = attachments.source.load;
  attachments.source.load = () => prisma;
  try {
    return await fn();
  } finally {
    attachments.source.load = load;
  }
}

test("run-code sends the chat's attachments, titles and file names only, and lists them", async () => {
  const prisma = fakePrisma([row("data.csv", "data.csv-1a2b.json"), row("Q3 report.pdf", "q3-report.pdf-3c4d.json")]);
  const runner = await fakeRunner({ ...done, attachments: ["Q3_report.pdf.txt", "data.csv"], attachment_notes: ["b.csv is over 50 MB, so it wasn't copied"] });
  try {
    const reply = await withPrisma(prisma, () => runCode.handler.call(chat().self, { language: "bash", code: "wc -l /work/attachments/data.csv" }));
    assert.equal(
      reply,
      "exit code: 0 (0.4s)\nstdout:\n3\nattachments in /work/attachments: Q3_report.pdf.txt, data.csv\n" +
        "attachments: b.csv is over 50 MB, so it wasn't copied"
    );
    assert.deepEqual(prisma.queries, [
      { where: { workspaceId: 7, threadId: 12, userId: 3 }, orderBy: { id: "asc" }, take: 51, select: { metadata: true } },
    ]);
    assert.deepEqual(runner.requests[0].args, {
      scope: { workspace: "career", thread: "12" },
      language: "bash",
      code: "wc -l /work/attachments/data.csv",
      timeout: 60,
      attachments: [
        { title: "data.csv", file: "data.csv-1a2b.json" },
        { title: "Q3 report.pdf", file: "q3-report.pdf-3c4d.json" },
      ],
      attachments_known: true,
    });
  } finally {
    await runner.close();
  }
});

test("the workspace's main chat asks for files with no thread, and single-user mode for no user", async () => {
  const prisma = fakePrisma([]);
  const { self } = chat({ workspace: { id: 7, slug: "career" }, thread_id: null, user_id: null });
  assert.deepEqual(await withPrisma(prisma, () => attachments.attachmentArgs(self)), { attachments: [], attachments_known: true });
  assert.deepEqual(prisma.queries[0].where, { workspaceId: 7, threadId: null });
});

test("API, Telegram and scheduled job runs have no chat, and no lookup", async () => {
  const prisma = fakePrisma([row("data.csv", "data.csv-1a2b.json")]);
  for (const invocation of [{ workspace: { id: 7, slug: "career" }, workspace_id: 7 }, {}, { thread_id: 3 }]) {
    assert.deepEqual(await withPrisma(prisma, () => attachments.attachmentArgs(chat(invocation).self)), {});
  }
  assert.deepEqual(prisma.queries, []);
});

test("a failed lookup sends nothing about attachments, so the runner removes nothing", async () => {
  const runner = await fakeRunner(done);
  try {
    // AnythingLLM's server not there to require (as on the host).
    const missing = chat();
    const gone = { load: () => require(path.join(os.tmpdir(), "no-anythingllm", "utils", "prisma")) };
    assert.equal(
      await withPrisma(null, () => {
        attachments.source.load = gone.load;
        return runCode.handler.call(missing.self, { language: "bash", code: "ls" });
      }),
      "exit code: 0 (0.4s)\nstdout:\n3"
    );
    assert.match(missing.logs[0], /couldn't look up the chat's attachments/);
    const broken = chat();
    await withPrisma(fakePrisma([], { fail: true }), () => runCode.handler.call(broken.self, { language: "bash", code: "ls" }));
    assert.match(broken.logs[0], /database is locked/);
    for (const { args } of runner.requests) {
      assert.ok(!("attachments" in args) && !("attachments_known" in args), JSON.stringify(args));
    }
  } finally {
    await runner.close();
  }
});

test("past MAX attachments, the list goes without saying it's whole", async () => {
  const rows = Array.from({ length: 60 }, (_, i) => row(`f${i}.csv`, `f${i}.csv-${i}.json`));
  const args = await withPrisma(fakePrisma(rows), () => attachments.attachmentArgs(chat().self));
  assert.equal(args.attachments.length, attachments.MAX);
  assert.ok(!("attachments_known" in args));
});

test("rows without a location are left out, and long titles cut", async () => {
  const rows = [{ metadata: "not json" }, { metadata: null }, { metadata: JSON.stringify({ title: "x" }) }, row("t".repeat(900), "a-1.json"), row("", "b-2.json")];
  const args = await withPrisma(fakePrisma(rows), () => attachments.attachmentArgs(chat().self));
  assert.deepEqual(args, { attachments: [{ title: "t".repeat(500), file: "a-1.json" }, { title: "b-2.json", file: "b-2.json" }], attachments_known: true });
});

test("a delegated task is refused before AnythingLLM's database is touched", async () => {
  let loaded = false;
  const load = attachments.source.load;
  attachments.source.load = () => {
    loaded = true;
    return fakePrisma([]);
  };
  try {
    const reply = await runCode.handler.call(chat({ workspace: { id: 9, slug: "agents-worker" }, thread_id: null, user_id: null }).self, { language: "bash", code: "ls" });
    assert.match(reply, /^Error: this tool isn't available to a delegated task/);
    assert.equal(loaded, false);
  } finally {
    attachments.source.load = load;
  }
});

// Only inside AnythingLLM's image (`hostctl test-skills`, or podman run … node --test): what
// the lookup leans on is there.
const inImage = fs.existsSync(attachments.PRISMA + "/index.js");
test("AnythingLLM's image has the table and the model the lookup relies on", { skip: !inImage && "not in AnythingLLM's image" }, () => {
  const schema = fs.readFileSync("/app/server/prisma/schema.prisma", "utf8");
  const model = schema.match(/model workspace_parsed_files \{([^}]*)\}/)?.[1] || "";
  for (const field of [/workspaceId\s+Int\b/, /userId\s+Int\?/, /threadId\s+Int\?/, /metadata\s+String\?/]) assert.match(model, field);
  const prisma = require(attachments.PRISMA);
  assert.equal(typeof prisma.workspace_parsed_files?.findMany, "function");
  const { WorkspaceParsedFiles } = require("/app/server/models/workspaceParsedFiles");
  assert.equal(typeof WorkspaceParsedFiles.where, "function");
  // AnythingLLM's own lookup of a chat's files is the same query.
  assert.match(WorkspaceParsedFiles.getContextFiles.toString(), /threadId: thread\?\.id \|\| null/);
});
