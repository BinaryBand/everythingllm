// The files a user attached in an AnythingLLM chat, for run-code: sandbox-runner copies
// their text into /work/attachments (packages/sandbox, sync_attachments).
//
// AnythingLLM keeps no attached file itself, only its text, as
// storage/direct-uploads/<name>-<uuid>.json, and a row in workspace_parsed_files for each
// (workspaceId, threadId: null in the workspace's main chat, userId, and metadata with the
// file's title and location). Skills run inside AnythingLLM's server, so this asks its
// database through the server's own Prisma client. Only the chat's own rows, and only their
// titles and file names go to the runner (a hostrpc line is at most 1 MiB): the runner
// reads the text from the uploads folder itself.
//
// The lookup is AnythingLLM's internals, not an API: if it fails, the run goes ahead
// without attachments, and without saying the list is whole, so the runner removes
// nothing. A test in the container (test/attachments.test.js) holds the image to it.

const path = require("path");
const { uiInvocation } = require("./scope");

const MAX = 50; // as sandbox.runner.ATTACHMENTS_MAX
const TITLE_MAX = 500;
// Not WorkspaceParsedFiles.where, which answers [] when the query fails: that would read as
// "nothing attached", and the runner would remove the chat's copies.
const PRISMA = "/app/server/utils/prisma";

// Where the database comes from; the tests swap it for a fake.
const source = { load: () => require(PRISMA) };

/** The chat a skill call came from: a chat in AnythingLLM's UI has a row of its own, with
 *  thread_id and user_id (null in the main chat and in single-user mode); API, Telegram
 *  and scheduled job runs have neither, and get no attachments. */
function chatOf(self) {
  const invocation = uiInvocation(self);
  const workspaceId = invocation?.workspace?.id;
  if (!Number.isInteger(workspaceId)) return null;
  const threadId = Number.isInteger(invocation.thread_id) ? invocation.thread_id : null;
  const userId = Number.isInteger(invocation.user_id) ? invocation.user_id : null;
  return { workspaceId, threadId, userId };
}

/**
 * The run op's attachment arguments for this call: {attachments: [{title, file}],
 * attachments_known: true} after a whole lookup, {attachments} alone when the chat has
 * more than MAX, and {} when it isn't a chat or the lookup failed. Never throws.
 */
async function attachmentArgs(self) {
  const chat = chatOf(self);
  if (!chat) return {};
  try {
    const prisma = source.load();
    const rows = await prisma.workspace_parsed_files.findMany({
      where: {
        workspaceId: chat.workspaceId,
        threadId: chat.threadId,
        ...(chat.userId === null ? {} : { userId: chat.userId }),
      },
      orderBy: { id: "asc" },
      take: MAX + 1,
      select: { metadata: true },
    });
    const attachments = [];
    for (const row of rows.slice(0, MAX)) {
      let metadata;
      try {
        metadata = JSON.parse(row.metadata || "{}");
      } catch {
        continue;
      }
      const file = typeof metadata?.location === "string" ? path.posix.basename(metadata.location) : "";
      if (!file) continue;
      const title = typeof metadata.title === "string" && metadata.title.trim() ? metadata.title : file;
      attachments.push({ title: title.slice(0, TITLE_MAX), file });
    }
    return rows.length > MAX ? { attachments } : { attachments, attachments_known: true };
  } catch (e) {
    self.logger?.(`run-code: couldn't look up the chat's attachments: ${e?.message || e}`);
    return {};
  }
}

module.exports = { attachmentArgs, source, MAX, PRISMA };
