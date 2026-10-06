// Write File: writes or deletes a file in the sandbox (src/mcps/sandbox), so long text doesn't
// have to go through a script, and a sandbox over its size limit can still be cleaned up.

const { withSandbox } = require("../_lib/sandbox");

module.exports.runtime = {
  handler: async function ({ path, content, delete: del }) {
    return withSandbox(this, async (request) => {
      const r = await request("write", { path: path ?? "", content: content ?? "", delete: del === true });
      if (r === null) return "The chat closed.";
      if (r.emptied) return `emptied ${r.path}`;
      if ("folder" in r) return `deleted ${r.folder ? "folder" : "file"} ${r.path}`;
      return `wrote ${r.path} (${r.bytes} bytes)`;
    });
  },
};
