// App: the workspace's apps (packages/sandbox's app op, sandbox.apps). An app is a template
// from the repo plus this workspace's data for it: this skill changes the data in one call,
// and the runner renders the page and moves the app's live card on. Lists are the first
// template.

const { withSandbox } = require("../_lib/sandbox");
const { asObject } = require("../_lib/runner");

function sentence(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) + "." : "";
}

module.exports.runtime = {
  handler: async function ({ action, name, title, op, item, items }) {
    return withSandbox(this, async (request) => {
      const args = {};
      if (item != null && item !== "") args.item = item;
      const list = asObject(items);
      if (Array.isArray(list) && list.length) args.items = list;
      if (op === "rename" && title) args.title = title;
      const r = await request("app", {
        action: action || "list",
        name: name ?? "",
        title: title ?? "",
        op: op ?? "",
        args,
      });
      if (r.apps)
        return r.apps.length
          ? ["This workspace's apps:", ...r.apps.map((a) => (a.error ? `- ${a.name}: ${a.error}` : `- ${a.name}: ${a.title}, ${a.summary}`))].join("\n")
          : "This workspace has no apps yet.";
      if (r.deleted) return `Deleted the app ${r.name}: its data, page and card are gone.`;
      return [`${sentence(r.did)} ${r.title}: ${r.summary}.`.trim(), `Card: ${r.card}`, `Page: ${r.page}`].join("\n");
    });
  },
};
