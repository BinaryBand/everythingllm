// Write File: writes or deletes a file in the sandbox (packages/sandbox), so long text doesn't
// have to go through a script, and a sandbox over its size limit can still be cleaned up.

const { withSandbox, publishedLines } = require("../_lib/sandbox");

module.exports.runtime = {
  handler: async function ({ path, content, delete: del }) {
    return withSandbox(this, async (request) => {
      const r = await request("write", { path: path ?? "", content: content ?? "", delete: del === true });
      const done = r.emptied
        ? `emptied ${r.path}`
        : "folder" in r
          ? `deleted ${r.folder ? "folder" : "file"} ${r.path}`
          : `wrote ${r.path} (${r.bytes} bytes)`;
      return [done, ...publishedLines(r.published)].join("\n");
    });
  },
};
