// Sandbox Access: shows whether this workspace's runs can reach the web, and turns that on or
// off (packages/sandbox's access op). Off works from any chat. On works only from a chat in
// AnythingLLM's UI, once the user approves it in AnythingLLM's own prompt
// (requestToolApproval): anywhere else AnythingLLM answers "approved" without asking anyone
// (a scheduled job, a skill set to run without asking, a channel with no prompt), so only
// its "the user approved" answer counts.

const { withSandbox } = require("../_lib/sandbox");
const { asFlag } = require("../_lib/runner");
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

function state(web) {
  return web
    ? "web access is on: this workspace's runs can reach public websites (never other workspaces' /shared folders)"
    : "web access is off: this workspace's runs reach only PyPI";
}

module.exports.runtime = {
  handler: async function ({ web, apply }) {
    const wanted = onOff(web);
    if (wanted === undefined) return 'Error: web must be "on" or "off".';
    return withSandbox(this, async (request) => {
      const change = wanted === null ? {} : { web: wanted };
      const shown = await request("access", change);
      if (shown === null) return "The chat closed.";
      if (!shown.would) return `In this workspace, ${state(shown.web)}.`;
      if (!asFlag(apply))
        return (
          `In this workspace, ${state(shown.web)}. Calling again with apply true would turn it ` +
          `${shown.would.web ? "on, after the user approves it in the chat" : "off"}.`
        );
      let approved = false;
      if (shown.needs_approval) {
        if (!uiInvocation(this))
          return (
            "Error: web access can only be turned on from a chat in AnythingLLM's own window, where the user " +
            "approves it. Tell the user to ask there."
          );
        if (typeof this.requestToolApproval !== "function")
          return "Error: AnythingLLM gave this skill no way to ask the user, so web access stays off.";
        const answer = await this.requestToolApproval({
          description:
            "Let this workspace's code runs reach public websites. Pages they read could try to instruct " +
            "the agent, and a run could send this workspace's files to any website. Runs with web access " +
            "can't see other workspaces' /shared folders. You can turn it off any time.",
          payload: { workspace: shown.workspace, web: "on" },
        });
        if (!answer?.approved) return `Web access stays off: ${answer?.message || "the user didn't approve it"}.`;
        if (answer.message !== USER_APPROVED)
          return (
            `Web access stays off: AnythingLLM approved it without asking the user (${answer.message}). ` +
            "If Sandbox Access is set to run without asking, the user can turn that off in Agent Skills and ask again."
          );
        approved = true;
      }
      const done = await request("access", { ...change, apply: true, approved });
      if (done === null) return "The chat closed.";
      return `Done. In this workspace, ${state(done.web)}.`;
    });
  },
};
