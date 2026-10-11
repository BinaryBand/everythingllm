// The take-over view's page (browser.takeover): noVNC showing the workspace's browser,
// view-only while the agent has it, with a dot in the colour of who has it (the body's
// class), the page's address and its last step, and the button that takes it or hands it
// back. Under the screen, a card for each thing that waits for you: the agent's requests to
// use a saved login or for one it hasn't got, and offers to save what you logged in with;
// while one waits, its buttons are the only way on, so take over and hand back aren't
// drawn. The workspace's saved logins and passkeys open in a panel. Everything from the
// server goes in as text, never as HTML: a username can come from a web page. While the
// browser is yours, the keyboard button opens a field that types into it as text (POST
// type), so a phone's keyboard, a paste and a password manager work where the canvas takes
// none of them; on a phone it opens as you take over. On a phone the buttons move from the
// bar to the bottom, and the saved logins open as a sheet.
import RFB from "./novnc/core/rfb.js";

const base = location.pathname.replace(/[^/]*$/, ""); // /<token>/
const tab = new URLSearchParams(location.search).get("tab") || ""; // the tab brought to the front
const $ = (id) => document.getElementById(id);
const scheme = location.protocol === "https:" ? "wss://" : "ws://";

let rfb = null;
let control = "agent";
let gone = false; // the screen's connection ended: only a reload brings it back
let shownState = {}; // the state last drawn, drawn again as the connection ends
let drawn = ""; // and as the runner sent it, so a poll that brings the same again draws nothing

// Scaled to fit a phone, the 1280 px screen's text is a few pixels high and nothing can be
// tapped. So below NARROW, while the browser is yours, the view shows it at its own size and
// a drag pans it (a tap still clicks); the button fits it to the view instead, or back.
// While the agent has it, it's only to watch, and fits.
const NARROW = window.matchMedia("(max-width: 800px)");
const TOUCH = window.matchMedia("(pointer: coarse)");
let fit = false; // the user's choice while it's theirs on a phone

function size() {
  const scaled = !NARROW.matches || control !== "user" || gone || fit;
  $("fit").setAttribute("aria-pressed", String(fit));
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
    render(shownState, drawn);
  });
}

// Who has the browser, and what's being done with it (Runner.activity).
const WHO = {
  working: "The agent is browsing",
  idle: "The agent has the tab, idle",
  waiting: "The agent has the tab",
  user: "You have the tab",
};

// The tab the view is about: the one last brought to the front, or the only one open.
function shown(s) {
  const tabs = s.tabs || [];
  return tabs.find((t) => t.id === s.front) || (tabs.length === 1 ? tabs[0] : null);
}

let typing = false; // the typing field is open (only while the browser is yours)

function show(s) {
  control = s.control;
  if (rfb) rfb.viewOnly = control !== "user";
  const mine = control === "user" && !gone;
  if (!mine) typing = false;
  $("state").textContent = gone ? "Disconnected" : WHO[mine ? "user" : s.state] || WHO.idle;
  const t = shown(s);
  const count = (s.tabs || []).length;
  $("url").textContent = t ? t.url || t.title : count ? `${count} tabs open` : "No page open";
  $("url").title = t?.url || "";
  $("caption").textContent = !mine && !gone && t?.last ? `Last step: ${t.last}` : "";
  // A request waiting for your answer is the only way on.
  const asked = [s.approvals, s.asked, s.offers].some((x) => x?.length);
  $("take").hidden = mine || gone || asked;
  $("give").hidden = !mine || asked;
  $("keyboard").hidden = $("fit").hidden = $("reason").hidden = !mine;
  $("typing").hidden = !typing;
  says($("reason"), s.waiting && s.reason ? `The agent asked: ${s.reason}` : "The agent waits while you have the tab.");
  $("gone").hidden = !gone;
  document.body.classList.remove("connecting");
  document.body.classList.toggle("mine", mine);
  document.body.classList.toggle("gone", gone);
  size();
}

// A card's text: what waits, and a line under it.
function text(main, sub) {
  return el("span", { className: "text" }, el("span", { textContent: main }), ...(sub ? [el("span", { textContent: sub })] : []));
}

function says(notice, main, sub) {
  notice.firstElementChild.replaceWith(text(main, sub));
}

// A switch (role=switch) for `label`, flipped by a click unless `onflip` says otherwise.
function toggle(label, on, onflip) {
  const b = el("button", { type: "button", className: "switch" });
  b.setAttribute("role", "switch");
  b.setAttribute("aria-label", label);
  set(b, on);
  b.onclick = () => (onflip ? onflip(b) : set(b, !isOn(b)));
  return b;
}
const isOn = (b) => b.getAttribute("aria-checked") === "true";
const set = (b, on) => b.setAttribute("aria-checked", String(!!on));
const TRASH =
  '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2.5 4.5h11 M6 4.5V3h4v1.5 M4 4.5l.6 8.5h6.8l.6-8.5"></path></svg>';

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
function problem(error) {
  says($("problem"), error || "");
  $("problem").hidden = !error;
}

// A POST for a button, its error (if any) shown as `doing`.
async function act(doing, what, body) {
  const error = await post(what, body);
  problem(error && `Couldn't ${doing}: ${error}`);
  return error;
}

// Cards kept by id in `box`: those gone go, new ones are made by `make` and added, and the
// rest stay as they are (with what's being typed in them).
function keyed(box, items, make) {
  const keep = new Set(items.map((x) => x.id));
  for (const old of [...box.children]) if (!keep.has(old.dataset.id)) old.remove();
  for (const x of items) {
    if (box.querySelector(`[data-id="${x.id}"]`)) continue;
    const row = make(x);
    row.dataset.id = x.id;
    box.append(row);
  }
}

// The agent's waits for your OK to use a saved login, one per chat, in the order asked.
function showApprovals(approvals) {
  keyed($("approvals"), approvals, (a) => {
    const chat = a.chat ? `the chat browsing “${a.chat}”` : "one of this workspace's chats";
    const allow = el("button", { textContent: "Allow", className: "primary" });
    const deny = el("button", { textContent: "Don't allow", className: "quiet" });
    allow.onclick = () => act("allow the request", `approve/${a.id}`);
    deny.onclick = () => act("refuse the request", `deny/${a.id}`);
    return el(
      "div",
      { className: "notice" },
      text(`The agent wants to use your ${a.kind} for ${who(a)}.`, `From ${chat}, on ${a.url}.`),
      el("div", { className: "acts" }, el("div", { className: "pair" }, allow, deny))
    );
  });
}

// The agent's requests for logins it has none of: each links to its own form.
function showAsked(asked) {
  $("asked").replaceChildren(
    ...asked.map((a) =>
      el(
        "div",
        { className: "notice" },
        text(`The agent needs your login for ${a.site}.`),
        el(
          "div",
          { className: "acts" },
          el("a", {
            href: a.link, textContent: "Enter login", className: "primary", target: "_blank", rel: "noopener noreferrer",
          })
        )
      )
    )
  );
}

function showOffers(offers) {
  keyed($("offers"), offers, (o) => {
    const name = el("input", { value: o.username, placeholder: "Username", autocomplete: "off" });
    name.setAttribute("aria-label", "Username");
    const ask = toggle("Ask me before each use", false);
    const save = el("button", { textContent: "Save login", className: "primary" });
    const drop = el("button", { textContent: "Not now", className: "quiet" });
    save.onclick = () =>
      act(`save the login for ${o.site}`, `offers/${o.id}/save`, { username: name.value, ask: isOn(ask) });
    drop.onclick = () => act(`drop the login for ${o.site}`, `offers/${o.id}/drop`);
    return el(
      "div",
      { className: "notice" },
      text(`Save the login you just used on ${o.site}?`),
      el(
        "div",
        { className: "acts" },
        name,
        el("div", { className: "ask" }, el("span", { textContent: "Ask me before each use" }), ask),
        el("div", { className: "pair" }, save, drop)
      )
    );
  });
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
    const ask = toggle(`Ask first for ${who(l)}`, l.ask, async (b) => {
      const on = !isOn(b);
      set(b, on);
      if (await act(`change the login for ${who(l)}`, `logins/${l.id}/ask`, { ask: on })) set(b, !on);
    });
    const kind = l.kind;
    const remove = el("button", { type: "button", className: "icon", title: "Delete", innerHTML: TRASH });
    remove.setAttribute("aria-label", `Delete the ${kind} for ${who(l)}`);
    remove.onclick = () =>
      confirm(`Delete the ${kind} for ${who(l)}?`) && act(`delete the ${kind} for ${who(l)}`, `logins/${l.id}/delete`);
    const extra = kind === "passkey" ? " · passkey" : "";
    const line = `${l.username || "(no username)"}${extra}${l.used ? ` · used ${l.used}` : ""}`;
    list.append(
      el(
        "li",
        {},
        el("div", { className: "who" }, el("strong", { textContent: l.site }), el("span", { textContent: line, title: line })),
        ask,
        remove
      )
    );
  }
}

// Making a passkey: only while you have the browser, and the site makes it on your click.
// While the view waits for one, a card under the screen says so too, with the same button.
function showMaking(s) {
  const mine = s.control === "user" && !gone;
  $("make").hidden = !mine;
  $("make").textContent = s.making ? "Stop waiting" : "Make a passkey";
  $("make").onclick = $("stop").onclick = () => act("make a passkey", "passkeys/make", { on: !s.making });
  $("making").hidden = !(mine && s.making);
  $("make-note").textContent = !mine
    ? s.made || "Take over to make a passkey."
    : s.making
      ? "Add one on the site's page."
      : s.made || "";
}

async function refresh() {
  try {
    const r = await fetch(base + "state", { cache: "no-store" });
    if (!r.ok) return;
    const raw = await r.text();
    if (raw !== drawn) render(JSON.parse(raw), raw);
  } catch {}
}

function render(s, raw) {
  shownState = s;
  drawn = raw;
  show(s);
  showApprovals(s.approvals || []);
  showAsked(s.asked || []);
  showOffers(s.offers || []);
  showLogins(s.logins || []);
  showMaking(s);
  // The last step shows only while nothing waits for you.
  $("caption").hidden = [...$("notices").querySelectorAll(".notice")].some((n) => !n.closest("[hidden]"));
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
  if (r.ok) render(data, raw);
  if (what === "take" && !$("typing").contains(document.activeElement)) rfb?.focus();
  return r.ok ? null : data.error || raw.trim() || `${r.status}`; // some refusals are plain text
}

// The typing field, focused. A phone opens its keyboard only for a focus in the tap's own
// handler, so this runs before anything is awaited.
function keyboard() {
  typing = true;
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
  fit = !fit;
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

// The saved logins, a panel opened from the bar, showing their list or the form that adds
// one; the focus goes with it.
function logins(open, adding = false) {
  $("logins").hidden = $("scrim").hidden = !open;
  $("logins-open").setAttribute("aria-expanded", String(open));
  $("logins-list").hidden = adding;
  $("add").hidden = !adding;
  if (!open) $("logins-open").focus();
  else if (adding) $("add").site.focus();
  else $("logins-list").querySelector(".close").focus();
}
$("logins-open").addEventListener("click", () => logins($("logins").hidden));
for (const close of $("logins").querySelectorAll(".close")) close.addEventListener("click", () => logins(false));
$("add-open").addEventListener("click", () => logins(true, true));
$("add-back").addEventListener("click", () => logins(true));
$("add-ask").addEventListener("click", (e) => set(e.currentTarget, !isOn(e.currentTarget)));
$("scrim").addEventListener("click", () => logins(false));
$("logins").addEventListener("keydown", (e) => e.key === "Escape" && logins(false));
$("give").addEventListener("click", () => act("hand back", "give"));
$("add").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const error = await post("logins", {
    site: f.site.value, username: f.username.value, password: f.password.value,
    ask: isOn($("add-ask")),
  });
  $("add-error").textContent = error || "";
  if (error) return;
  f.reset();
  set($("add-ask"), false);
  logins(true);
});
place();
connect();
refresh();
// The state every 2 s while the page is in view, and as soon as it's back in view.
const visible = () => document.visibilityState !== "hidden";
setInterval(() => visible() && refresh(), 2000);
document.addEventListener("visibilitychange", () => visible() && refresh());
