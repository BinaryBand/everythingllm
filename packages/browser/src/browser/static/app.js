// The take-over view's page (browser.takeover): noVNC showing the workspace's browser,
// view-only while the agent has it, and the buttons that take it and hand it back; the
// agent's requests to use a saved login or for one it hasn't got, offers to save what you
// logged in with, and the workspace's saved logins. Everything from the server goes in as text, never as HTML: a
// username can come from a web page.
import RFB from "./novnc/core/rfb.js";

const base = location.pathname.replace(/[^/]*$/, ""); // /<token>/
const $ = (id) => document.getElementById(id);
const scheme = location.protocol === "https:" ? "wss://" : "ws://";

let rfb = null;
let control = "agent";

function connect() {
  rfb = new RFB($("screen"), scheme + location.host + base + "websockify", { wsProtocols: ["binary"] });
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.viewOnly = control !== "user";
  rfb.addEventListener("connect", () => refresh());
  rfb.addEventListener("disconnect", () => {
    $("state").textContent = "Disconnected. Reload the page to see the browser again.";
    $("take").hidden = $("give").hidden = true;
  });
}

function show(s) {
  control = s.control;
  if (rfb) rfb.viewOnly = control !== "user";
  const mine = control === "user";
  $("state").textContent = mine ? "You have the browser." : "The agent has the browser; you're watching.";
  $("take").hidden = mine;
  $("give").hidden = !mine;
  const why = mine && s.waiting && s.reason ? `The agent asked: ${s.reason}` : "";
  $("reason").textContent = why;
  $("reason").hidden = !why;
  document.body.classList.toggle("mine", mine);
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  node.append(...children);
  return node;
}

function who(x) {
  return x.username ? `${x.username} on ${x.site}` : x.site;
}

// What went wrong with the last thing you did, until the next thing works. Kept out of the
// rows: the poll rebuilds the logins and drops an offer that's gone.
function problem(text) {
  $("problem").textContent = text || "";
  $("problem").hidden = !text;
}

// A POST for a button, its error (if any) shown as `doing`.
async function act(doing, what, body) {
  const error = await post(what, body);
  problem(error && `Couldn't ${doing}: ${error}`);
  return error;
}

function showApproval(a) {
  $("approval").hidden = !a;
  if (!a) return;
  $("approval-text").textContent = `The agent wants to use your login for ${who(a)}.`;
  $("allow").onclick = () => act("allow the request", `approve/${a.id}`);
  $("deny").onclick = () => act("refuse the request", `deny/${a.id}`);
}

// The agent's requests for logins it has none of: each links to its own form.
function showAsked(asked) {
  $("asked").replaceChildren(
    ...asked.map((a) =>
      el(
        "div",
        {},
        el("span", { textContent: `The agent asked for your login for ${a.site}.` }),
        el("a", { href: a.link, textContent: "Enter it", target: "_blank", rel: "noopener noreferrer" })
      )
    )
  );
}

function showOffers(offers) {
  const box = $("offers");
  const keep = new Set(offers.map((o) => o.id));
  for (const old of [...box.children]) if (!keep.has(old.dataset.id)) old.remove();
  for (const o of offers) {
    if (box.querySelector(`[data-id="${o.id}"]`)) continue; // keep what's being typed
    const name = el("input", { value: o.username, placeholder: "Username", autocomplete: "off" });
    const ask = el("input", { type: "checkbox" });
    const save = el("button", { textContent: "Save login" });
    const drop = el("button", { textContent: "Not now", className: "quiet" });
    const row = el(
      "div",
      { className: "ask" },
      el("span", { textContent: `Save the login you just used on ${o.site}?` }),
      name,
      el("label", {}, ask, " Ask me before each use"),
      save,
      drop
    );
    row.dataset.id = o.id;
    save.onclick = () =>
      act(`save the login for ${o.site}`, `offers/${o.id}/save`, { username: name.value, ask: ask.checked });
    drop.onclick = () => act(`drop the login for ${o.site}`, `offers/${o.id}/drop`);
    box.append(row);
  }
}

function showLogins(logins) {
  const list = $("login-list");
  list.replaceChildren();
  if (!logins.length) list.append(el("li", { textContent: "None yet.", className: "note" }));
  for (const l of logins) {
    if (l.error) {
      list.append(el("li", { textContent: l.error, className: "error" }));
      continue;
    }
    const ask = el("input", { type: "checkbox", checked: l.ask });
    ask.onchange = async () => {
      if (await act(`change the login for ${who(l)}`, `logins/${l.id}/ask`, { ask: ask.checked })) {
        ask.checked = !ask.checked;
      }
    };
    const remove = el("button", { textContent: "Delete", className: "quiet" });
    remove.onclick = () =>
      confirm(`Delete the login for ${who(l)}?`) && act(`delete the login for ${who(l)}`, `logins/${l.id}/delete`);
    list.append(
      el(
        "li",
        {},
        el("strong", { textContent: l.site }),
        ` ${l.username || "(no username)"}${l.totp ? " · 2FA" : ""}${l.used ? ` · used ${l.used}` : ""} `,
        el("label", {}, ask, " ask first"),
        remove
      )
    );
  }
}

async function refresh() {
  try {
    const r = await fetch(base + "state", { cache: "no-store" });
    if (r.ok) render(await r.json());
  } catch {}
}

function render(s) {
  show(s);
  showApproval(s.approval);
  showAsked(s.asked || []);
  showOffers(s.offers || []);
  showLogins(s.logins || []);
}

async function post(what, body) {
  let r;
  try {
    r = await fetch(base + what, {
      method: "POST",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    return "couldn't reach the browser runner";
  }
  const raw = await r.text().catch(() => "");
  let data = {};
  try {
    data = JSON.parse(raw);
  } catch {}
  if (r.ok) render(data);
  if (what === "take") rfb?.focus();
  return r.ok ? null : data.error || raw.trim() || `${r.status}`; // some refusals are plain text
}

$("take").addEventListener("click", () => act("take over", "take"));
$("give").addEventListener("click", () => act("hand back", "give"));
$("add").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const error = await post("logins", {
    site: f.site.value, username: f.username.value, password: f.password.value,
    totp: f.totp.value, ask: f.ask.checked,
  });
  $("add-error").textContent = error || "";
  if (!error) f.reset();
});
connect();
refresh();
setInterval(refresh, 2000);
