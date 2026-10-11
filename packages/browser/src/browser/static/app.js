// The take-over view's page (browser.takeover): noVNC showing the workspace's browser,
// view-only while the agent has it, framed in the colour of who has it (the body's class),
// with the page's address and its last step, and the buttons that take it and hand it back; the
// agent's requests to use a saved login or for one it hasn't got, offers to save what you
// logged in with, and the workspace's saved logins and passkeys. Everything from the server goes in as text, never as HTML: a
// username can come from a web page. While the browser is yours, a field under the screen
// types into it as text (POST type), so a phone's keyboard, a paste and a password manager
// work where the canvas takes none of them; on a phone it's focused as you take over. On a
// phone the buttons move from the bar to the bottom, and the saved logins open as a sheet.
import RFB from "./novnc/core/rfb.js";

const base = location.pathname.replace(/[^/]*$/, ""); // /<token>/
const tab = new URLSearchParams(location.search).get("tab") || ""; // the tab brought to the front
const $ = (id) => document.getElementById(id);
const scheme = location.protocol === "https:" ? "wss://" : "ws://";

let rfb = null;
let control = "agent";
let gone = false; // the screen's connection ended: only a reload brings it back
let shownState = {}; // the state last drawn, drawn again as the connection ends

// Scaled to fit a phone, the 1280 px screen's text is a few pixels high and nothing can be
// tapped. So below NARROW the view shows it at its own size and a drag pans it (a tap still
// clicks once it's yours); the button fits it to the view instead, or back.
const NARROW = window.matchMedia("(max-width: 800px)");
const TOUCH = window.matchMedia("(pointer: coarse)");
let fit = null; // the user's choice, once they've made one

function size() {
  const scaled = fit ?? !NARROW.matches;
  $("fit").setAttribute("aria-pressed", String(scaled));
  document.body.classList.toggle("fit", scaled);
  if (!rfb) return;
  rfb.scaleViewport = scaled;
  rfb.clipViewport = !scaled;
  rfb.dragViewport = !scaled;
}

function connect() {
  rfb = new RFB($("screen"), scheme + location.host + base + "websockify", { wsProtocols: ["binary"] });
  size();
  rfb.resizeSession = false;
  rfb.background = "transparent";
  rfb.viewOnly = control !== "user";
  rfb.addEventListener("connect", () => refresh());
  rfb.addEventListener("disconnect", () => {
    gone = true;
    render(shownState);
  });
}

// Who has the browser, and what's being done with it (Runner.activity).
const WHO = {
  working: "The agent is browsing",
  idle: "The agent has the tab, idle",
  waiting: "The agent is waiting",
  user: "You have the tab",
};

// The tab the view is about: the one last brought to the front, or the only one open.
function shown(s) {
  const tabs = s.tabs || [];
  return tabs.find((t) => t.id === s.front) || (tabs.length === 1 ? tabs[0] : null);
}

function caption(s, t) {
  if (gone) return "The picture is the last one this view got.";
  if (s.control === "user") return "Your clicks and typing go to this tab.";
  const last = t?.last ? ` Last step: ${t.last}` : "";
  if (s.state === "waiting") return "View only. The agent goes on once you answer.";
  if (s.state === "idle") return `View only. The agent has the browser but isn't using it now.${last}`;
  return `View only while the agent has the tab.${last}`;
}

function show(s) {
  control = s.control;
  if (rfb) rfb.viewOnly = control !== "user";
  const mine = control === "user" && !gone;
  $("state").textContent = gone ? "Disconnected" : WHO[mine ? "user" : s.state] || WHO.idle;
  const t = shown(s);
  const count = (s.tabs || []).length;
  $("url").textContent = t ? t.url || t.title : count ? `${count} tabs open` : "No page open";
  $("url").title = t?.url || "";
  $("caption").textContent = caption(s, t);
  $("take").hidden = mine || gone;
  $("give").hidden = $("keyboard").hidden = $("typing").hidden = $("reason").hidden = !mine;
  $("reason").textContent =
    s.waiting && s.reason ? `The agent asked: ${s.reason}` : "The agent waits while you have the tab.";
  $("gone").hidden = !gone;
  document.body.classList.remove("connecting");
  document.body.classList.toggle("mine", mine);
  document.body.classList.toggle("gone", gone);
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

// The agent's waits for your OK to use a saved login, one per chat, in the order asked.
function showApprovals(approvals) {
  const box = $("approvals");
  const keep = new Set(approvals.map((a) => a.id));
  for (const old of [...box.children]) if (!keep.has(old.dataset.id)) old.remove();
  for (const a of approvals) {
    if (box.querySelector(`[data-id="${a.id}"]`)) continue;
    const chat = a.chat ? `the chat browsing “${a.chat}”` : "one of this workspace's chats";
    const allow = el("button", { textContent: "Allow", className: "primary" });
    const deny = el("button", { textContent: "Don't allow", className: "quiet" });
    const row = el(
      "div",
      { className: "notice ask" },
      el("span", { textContent: `The agent in ${chat} wants to use your ${a.kind} for ${who(a)}, on ${a.url}.` }),
      allow,
      deny
    );
    row.dataset.id = a.id;
    allow.onclick = () => act("allow the request", `approve/${a.id}`);
    deny.onclick = () => act("refuse the request", `deny/${a.id}`);
    box.append(row);
  }
}

// The agent's requests for logins it has none of: each links to its own form.
function showAsked(asked) {
  $("asked").replaceChildren(
    ...asked.map((a) =>
      el(
        "div",
        { className: "notice ask" },
        el("span", { textContent: `The agent needs your login for ${a.site} to go on.` }),
        el("a", {
          href: a.link, textContent: "Enter login", className: "quiet", target: "_blank", rel: "noopener noreferrer",
        })
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
    const save = el("button", { textContent: "Save login", className: "primary" });
    const drop = el("button", { textContent: "Not now", className: "quiet" });
    const row = el(
      "div",
      { className: "notice you" },
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
    const kind = l.kind;
    const remove = el("button", { textContent: "Delete", className: "quiet" });
    remove.onclick = () =>
      confirm(`Delete the ${kind} for ${who(l)}?`) && act(`delete the ${kind} for ${who(l)}`, `logins/${l.id}/delete`);
    const extra = kind === "passkey" ? " · passkey" : "";
    const line = `${l.username || "(no username)"}${extra}${l.used ? ` · used ${l.used}` : ""}`;
    list.append(
      el(
        "li",
        {},
        el("div", { className: "who" }, el("strong", { textContent: l.site }), el("span", { textContent: line, title: line })),
        el("label", {}, ask, "ask first"),
        remove
      )
    );
  }
}

// Making a passkey: only while you have the browser, and the site makes it on your click.
// While the view waits for one, a line under the bar says so too, with the same button.
const MAKING = "Waiting for the site to make one: add a passkey on its page now. It's saved here, asking first.";
function showMaking(s) {
  const mine = s.control === "user" && !gone;
  $("make").hidden = !mine;
  $("make").textContent = s.making ? "Stop waiting" : "Make a passkey";
  $("make").onclick = $("stop").onclick = () => act("make a passkey", "passkeys/make", { on: !s.making });
  $("making").hidden = !(mine && s.making);
  $("making").firstElementChild.textContent = MAKING;
  $("make-note").textContent = !mine
    ? s.made || "Take over the browser to make a passkey for a site."
    : s.making
      ? MAKING
      : s.made || "Press this, then add a passkey on the site's page. It's kept here, on this machine alone.";
}

async function refresh() {
  try {
    const r = await fetch(base + "state", { cache: "no-store" });
    if (r.ok) render(await r.json());
  } catch {}
}

function render(s) {
  shownState = s;
  show(s);
  showApprovals(s.approvals || []);
  showAsked(s.asked || []);
  showOffers(s.offers || []);
  showLogins(s.logins || []);
  showMaking(s);
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
  if (what === "take" && !$("typing").contains(document.activeElement)) rfb?.focus();
  return r.ok ? null : data.error || raw.trim() || `${r.status}`; // some refusals are plain text
}

// The typing field, focused. A phone opens its keyboard only for a focus in the tap's own
// handler, so this runs before anything is awaited.
function keyboard() {
  $("typing").hidden = false;
  $("type-text").focus();
  $("type-text").scrollIntoView({ block: "nearest" });
}

// Send a field's text to the browser's focused field, and empty it; it's put back if it
// didn't go. Nothing keeps it.
async function send(field) {
  const text = field.value;
  if (!text) return;
  field.value = "";
  const error = await act("type into the browser", "type", { text, secret: field.type === "password", tab });
  if (error && !field.value) field.value = text;
}

$("take").addEventListener("click", () => {
  if (NARROW.matches || TOUCH.matches) keyboard();
  act("take over", "take");
});
$("keyboard").addEventListener("click", keyboard);
$("typing").addEventListener("submit", (e) => e.preventDefault());
for (const field of [$("type-text"), $("type-password")]) {
  field.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.isComposing) return;
    e.preventDefault();
    send(field);
  });
}
for (const button of $("typing").querySelectorAll("button")) {
  button.addEventListener("mousedown", (e) => e.preventDefault()); // the field keeps focus, and a phone its keyboard
  button.addEventListener("click", () =>
    button.dataset.field ? send($(button.dataset.field)) : act(`press ${button.dataset.key}`, "key", { key: button.dataset.key, tab })
  );
}
$("fit").addEventListener("click", () => {
  fit = !(fit ?? !NARROW.matches);
  size();
});

// The buttons sit in the bar, or at the bottom on a phone.
function place() {
  if (NARROW.matches) $("row").append($("actions"));
  else $("logins-open").after($("actions"));
}
NARROW.addEventListener("change", () => {
  place();
  size();
});

// The saved logins, a panel opened from the bar; the focus goes with it.
function logins(open) {
  $("logins").hidden = $("scrim").hidden = !open;
  $("logins-open").setAttribute("aria-expanded", String(open));
  $(open ? "logins-close" : "logins-open").focus();
}
$("logins-open").addEventListener("click", () => logins($("logins").hidden));
$("logins-close").addEventListener("click", () => logins(false));
$("scrim").addEventListener("click", () => logins(false));
$("logins").addEventListener("keydown", (e) => e.key === "Escape" && logins(false));
$("give").addEventListener("click", () => act("hand back", "give"));
$("add").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const error = await post("logins", {
    site: f.site.value, username: f.username.value, password: f.password.value,
    ask: f.ask.checked,
  });
  $("add-error").textContent = error || "";
  if (!error) f.reset();
});
place();
connect();
refresh();
setInterval(refresh, 2000);
