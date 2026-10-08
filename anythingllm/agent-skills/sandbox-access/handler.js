// Sandbox Access: shows what this workspace's runs can reach beyond PyPI (the web; a model,
// with a daily token budget), and turns that on or off (packages/sandbox's access op). Off,
// and a lower budget, work from any chat. On, and a higher budget, work only from a chat in
// AnythingLLM's UI, once the user approves it in AnythingLLM's own prompt
// (requestToolApproval): anywhere else AnythingLLM answers "approved" without asking anyone
// (a scheduled job, a skill set to run without asking, a channel with no prompt), so only
// its "the user approved" answer counts.

const { withSandbox } = require("../_lib/sandbox");
const { asFlag, asInteger } = require("../_lib/runner");
const { uiInvocation } = require("../_lib/scope");

const USER_APPROVED = "User approved the tool execution.";

/** true for "on", false for "off", null when left out; undefined when it's neither. */
function onOff(value) {
  if (value == null || value === "") return null;
  if (value === true || value === false) return value;
  const word = String(value).trim().toLowerCase();
  if (["on", "true", "yes"].includes(word)) return true;
  if (["off", "false", "no"].includes(word)) return false;
  return undefined;
}

function state(a) {
  return [
    a.web
      ? "web access is on (runs can reach public websites, but not other workspaces' /shared folders)"
      : "web access is off (runs reach only PyPI)",
    a.models
      ? `model access is on (runs can ask a model, up to ${a.daily_tokens} tokens a day; ` +
        "from Python, `from everythingllm_models import ask`; from bash, `python3 /sandbox/everythingllm_models.py \"prompt\"`)"
      : "model access is off",
  ].join("; ");
}

/** What the user is asked to approve: only what's being turned on, and what it means. */
function approval(now, would) {
  const parts = [];
  if (would.web && !now.web)
    parts.push(
      "Let this workspace's code runs reach public websites. Pages they read could try to instruct the agent, " +
        "and a run could send this workspace's files to any website. Runs with web access can't see other " +
        "workspaces' /shared folders."
    );
  if (would.models && !now.models)
    parts.push(
      `Let this workspace's code runs ask a model on the server's account, up to ${would.daily_tokens} tokens a day. ` +
        "The key stays on the server."
    );
  else if (would.models && would.daily_tokens > now.daily_tokens)
    parts.push(`Raise this workspace's model budget from ${now.daily_tokens} to ${would.daily_tokens} tokens a day.`);
  parts.push("You can turn it off any time.");
  return parts.join(" ");
}

module.exports.runtime = {
  handler: async function ({ web, models, daily_tokens, apply }) {
    const wantWeb = onOff(web);
    const wantModels = onOff(models);
    if (wantWeb === undefined || wantModels === undefined) return 'Error: web and models must be "on" or "off".';
    const budget = daily_tokens == null || daily_tokens === "" ? null : asInteger(daily_tokens);
    if (budget !== null && !Number.isInteger(budget)) return "Error: daily_tokens must be a whole number.";
    return withSandbox(this, async (request) => {
      const change = {};
      if (wantWeb !== null) change.web = wantWeb;
      if (wantModels !== null) change.models = wantModels;
      if (budget !== null) change.daily_tokens = budget;
      const shown = await request("access", change);
      if (!shown.would) return `In this workspace, ${state(shown)}.`;
      if (!asFlag(apply))
        return (
          `In this workspace, ${state(shown)}. Calling again with apply true would make it: ${state(shown.would)}` +
          (shown.needs_approval ? ", after the user approves it in the chat." : ".")
        );
      let approved = false;
      if (shown.needs_approval) {
        if (!uiInvocation(this))
          return (
            "Error: sandbox access can only be turned on or raised from a chat in AnythingLLM's own window, where " +
            "the user approves it. Tell the user to ask there."
          );
        if (typeof this.requestToolApproval !== "function")
          return "Error: AnythingLLM gave this skill no way to ask the user, so nothing was turned on.";
        const answer = await this.requestToolApproval({
          description: approval(shown, shown.would),
          payload: { workspace: shown.workspace, ...change },
        });
        if (!answer?.approved) return `Nothing changed: ${answer?.message || "the user didn't approve it"}.`;
        if (answer.message !== USER_APPROVED)
          return (
            `Nothing changed: AnythingLLM approved it without asking the user (${answer.message}). ` +
            "If Sandbox Access is set to run without asking, the user can turn that off in Agent Skills and ask again."
          );
        approved = true;
      }
      const done = await request("access", { ...change, apply: true, approved });
      return `Done. In this workspace, ${state(done)}.`;
    });
  },
};
