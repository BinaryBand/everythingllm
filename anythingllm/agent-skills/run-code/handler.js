// Run Code: runs a script in sandbox-runner on the host (packages/sandbox) and replies with its
// output. Waits for the whole run, showing in the chat that it's still going; if the chat
// closes first, the run finishes on its own and its files stay. In the background it answers
// with the run's id once the runner's first wait is out, and a call with that run_id checks
// on it (the runner keeps a result for a day). The chat's attachments go
// with the run (_lib/attachments.js), and the runner puts their text in /work/attachments.

const { withSandbox, publishedLines } = require("../_lib/sandbox");
const { asFlag, asInteger } = require("../_lib/runner");
const { attachmentArgs } = require("../_lib/attachments");

function format(r) {
  const lines = [];
  if (r.timed_out) lines.push(`TIMED OUT after ${r.timeout}s and was killed`);
  else if (r.oom_killed) lines.push(`KILLED: ran out of memory (the limit is ${r.memory || "1g"}) after ${r.seconds}s`);
  else lines.push(`exit code: ${r.exit_code} (${r.seconds}s)`);
  if (r.stdout) lines.push("stdout:", r.stdout.replace(/\n$/, ""));
  if (r.stderr) lines.push("stderr:", r.stderr.replace(/\n$/, ""));
  if (!r.stdout && !r.stderr) lines.push("(no output)");
  if (r.changed.length)
    lines.push(`files created or changed: ${r.changed.join(", ")}${r.changed_more ? ` and ${r.changed_more} more` : ""}`);
  if (r.attachments?.length) lines.push(`attachments in /work/attachments: ${r.attachments.join(", ")}`);
  for (const note of r.attachment_notes || []) lines.push(`attachments: ${note}`);
  if (r.warning) lines.push(`warning: ${r.warning}`);
  if (r.web) lines.push("web access: on (this workspace's runs can reach public websites)");
  if (r.models) lines.push(`model access: on (${r.models.tokens_left} tokens left today)`);
  lines.push(...publishedLines(r.published));
  return lines.join("\n");
}

module.exports.runtime = {
  handler: async function ({ language, code, timeout, background, run_id }) {
    return withSandbox(this, async (request) => {
      if (run_id) {
        const r = await request("wait", { run_id: String(run_id) });
        if (r.running)
          return (
            `Run ${r.run_id} is still going: ${Math.round(r.seconds)} s so far (it's stopped at ${r.timeout} s). ` +
            `Check again later with run_id ${r.run_id}.`
          );
        return `Run ${r.run_id} finished.\n${format(r)}`;
      }
      // Only here, past withSandbox's refusal of a delegated task: it reads AnythingLLM's database.
      const attachments = await attachmentArgs(this);
      const seconds = asInteger(timeout);
      let r = await request("run", { language, code, ...(Number.isInteger(seconds) ? { timeout: seconds } : {}), ...attachments });
      if (r.running && asFlag(background))
        return (
          `Started in the background as run ${r.run_id} (stopped at ${r.timeout} s if it isn't done). ` +
          `Tell the user it's going; check on it with run-code and run_id ${r.run_id}. Its result is kept for a day. ` +
          "Until it ends, this workspace's other long runs and its file writes are refused."
        );
      while (r.running) {
        this.introspect(`Still running (${Math.round(r.seconds)} s)…`);
        r = await request("wait", { run_id: r.run_id });
      }
      return format(r);
    }, { closed: "The chat closed; the run carries on, and its files will be in /work." });
  },
};
