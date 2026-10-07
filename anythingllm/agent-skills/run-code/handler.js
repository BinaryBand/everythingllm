// Run Code: runs a script in sandbox-runner on the host (packages/sandbox) and replies with its
// output. Waits for the whole run, showing in the chat that it's still going; if the chat
// closes first, the run finishes on its own and its files stay. The chat's attachments go
// with the run (_lib/attachments.js), and the runner puts their text in /work/attachments.

const { withSandbox, publishedLines } = require("../_lib/sandbox");
const { attachmentArgs } = require("../_lib/attachments");

function format(r) {
  const lines = [];
  if (r.timed_out) lines.push(`TIMED OUT after ${r.timeout}s and was killed`);
  else if (r.oom_killed) lines.push(`KILLED: ran out of memory (the limit is 1 GB) after ${r.seconds}s`);
  else lines.push(`exit code: ${r.exit_code} (${r.seconds}s)`);
  if (r.stdout) lines.push("stdout:", r.stdout.replace(/\n$/, ""));
  if (r.stderr) lines.push("stderr:", r.stderr.replace(/\n$/, ""));
  if (!r.stdout && !r.stderr) lines.push("(no output)");
  if (r.changed.length)
    lines.push(`files created or changed: ${r.changed.join(", ")}${r.changed_more ? ` and ${r.changed_more} more` : ""}`);
  if (r.attachments?.length) lines.push(`attachments in /work/attachments: ${r.attachments.join(", ")}`);
  for (const note of r.attachment_notes || []) lines.push(`attachments: ${note}`);
  if (r.warning) lines.push(`warning: ${r.warning}`);
  lines.push(...publishedLines(r.published));
  return lines.join("\n");
}

module.exports.runtime = {
  handler: async function ({ language, code, timeout }) {
    return withSandbox(this, async (request) => {
      // Only here, past withSandbox's refusal of a delegated task: it reads AnythingLLM's database.
      const attachments = await attachmentArgs(this);
      let r = await request("run", { language, code, timeout: Number.isInteger(timeout) ? timeout : 60, ...attachments });
      while (r?.running) {
        this.introspect(`Still running (${Math.round(r.seconds)} s)…`);
        r = await request("wait", { run_id: r.run_id });
      }
      if (r === null) return "The chat closed; the run carries on, and its files will be in /work.";
      return format(r);
    });
  },
};
