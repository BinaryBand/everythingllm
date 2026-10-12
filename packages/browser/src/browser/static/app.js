// The take-over view's page (browser.takeover): noVNC showing the workspace's browser,
// view-only while the agent has it, with a dot in the colour of who has it (the body's
// class), the page's address, and the button that takes it or hands it back. Under the
// screen, one card: what waits for you (the agent's requests to use a saved login or for one
// it hasn't got, and offers to save what you logged in with), or the last step when nothing
// does; while one waits, its buttons are the only way on, so take over and hand back aren't
// drawn. The workspace's saved logins and passkeys open in a panel, where a row opens the
// login to change or remove it. Everything from the server goes in as text, never as HTML: a
// username can come from a web page. While the browser is yours, the keyboard button opens a
// field that types into it as text (POST type), so a phone's keyboard, a paste and a password
// manager work where the canvas takes none of them. On a phone the page fills the view under
// a header naming it: touching it takes it, the cards float over its foot (the last step as a
// chip), the buttons are a bar there while it's yours, and the saved logins open as a sheet.
// Tapping the title edits the address; Go loads it in the tab and takes the browser (POST
// open). A closed tab and a lost connection fill the screen's place with the way back.
//
// In AnywhereLLM's web view (a JavaScript channel named AnywhereLLM), the page is the app's
// take-over screen: Back returns to the chat, and a message bar takes the place of the
// buttons' bar. It opens as you take the tab, or from the round button while the agent has
// it. Empty, its button is Hand back; with text it sends it into the chat through the app
// (and hands back, while the tab is yours). A message sent while a request waits is held
// until it's answered. The app says how a message went through window.anywhereLLM.said.
import RFB from "./novnc/core/rfb.js";

const base = location.pathname.replace(/[^/]*$/, ""); // /<token>/
const params = new URLSearchParams(location.search);
const tab = params.get("tab") || ""; // the tab brought to the front
const query = tab ? `?tab=${encodeURIComponent(tab)}` : ""; // so the state says how it stands
const $ = (id) => document.getElementById(id);
const scheme = location.protocol === "https:" ? "wss://" : "ws://";
const app = window.AnywhereLLM; // the app's channel, in its web view
const inApp = typeof app?.postMessage === "function";
const theme = params.get("theme");
if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;

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

// Whether a request waits for your answer: then its buttons are the only way on.
const waits = (s) => [s.approvals, s.asked, s.offers].some((x) => x?.length);
const mine = () => control === "user" && !gone;
// The tab ?tab= names, once it's closed.
const closedTab = (s) => (!gone && s.tab && !s.tab.open ? s.tab : null);

let typing = false; // the typing field is open (only while the browser is yours)

function show(s) {
  control = s.control;
  const closed = closedTab(s);
  if (rfb) rfb.viewOnly = control !== "user";
  const yours = mine();
  if (!yours) typing = false;
  const t = shown(s);
  const count = (s.tabs || []).length;
  const none = count ? `${count} tabs open` : "No page open";
  $("state").textContent = gone
    ? "Disconnected"
    : closed
      ? "Tab closed"
      : WHO[yours ? "user" : s.state] || WHO.idle;
  // On a phone, under the page's title, only who.
  $("who").textContent = gone ? "Disconnected" : closed ? "" : yours ? "You have it" : "Agent has it";
  $("page").textContent = gone ? "Disconnected" : closed ? "Tab closed" : t?.title || t?.url || none;
  const url = closed ? closed.url : t?.url;
  $("url").textContent = url ? address(url) : t?.title || none;
  $("url").title = url || "";
  $("address-open").disabled = gone || !!closed || !t;
  says($("caption"), !yours && !gone && !closed && t?.last ? `Last step: ${t.last}` : "");
  const asked = waits(s);
  $("take").hidden = $("touch").hidden = yours || gone || !!closed || asked;
  $("give").hidden = !yours || asked;
  $("keyboard").hidden = $("fit").hidden = $("reason").hidden = !yours;
  $("typing").hidden = !typing;
  const reason = s.waiting && s.reason;
  says($("reason"), reason ? `The agent asked: ${reason}` : "The agent waits.");
  $("reason").classList.toggle("plain", !reason); // drawn only while nothing else waits, never on a phone
  $("ended").hidden = !(gone || closed);
  $("ended").firstElementChild.textContent = gone
    ? "The connection to the browser dropped."
    : "The agent's tab is closed.";
  $("ended").lastElementChild.textContent = gone ? "Reconnect" : "Open it again";
  document.body.classList.remove("connecting");
  document.body.classList.toggle("mine", yours);
  document.body.classList.toggle("gone", gone);
  document.body.classList.toggle("closed", !!closed);
  size();
}

// An address as shown: https:// goes without saying, http:// stays in sight.
const address = (url) => url.replace(/^https:\/\//, "");

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
const svg = (d) =>
  `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${d}"></path></svg>`;
const TRASH = svg("M2.5 4.5h11 M6 4.5V3h4v1.5 M4 4.5l.6 8.5h6.8l.6-8.5");
const FORWARD = svg("M6 4l4 4-4 4");

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
    const as = a.username ? `As ${a.username}, from` : "From";
    const allow = el("button", { textContent: "Allow", className: "primary" });
    const deny = el("button", { textContent: "Don't allow", className: "quiet" });
    allow.onclick = () => act("allow the request", `approve/${a.id}`);
    deny.onclick = () => act("refuse the request", `deny/${a.id}`);
    return el(
      "div",
      { className: "notice" },
      text(`The agent wants your ${a.kind} for ${a.site} for ${a.minutes} minutes.`, `${as} ${chat}, on ${address(a.url)}.`),
      el("div", { className: "acts" }, el("div", { className: "pair" }, deny, allow))
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
        el("span", { className: "opts" }, name, el("div", { className: "ask" }, el("span", { textContent: "Ask me before each use" }), ask)),
        el("div", { className: "pair" }, drop, save)
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
    const remove = el("button", { type: "button", className: "icon remove", title: "Delete", innerHTML: TRASH });
    remove.setAttribute("aria-label", `Delete the ${kind} for ${who(l)}`);
    remove.onclick = () =>
      confirm(`Delete the ${kind} for ${who(l)}?`) && act(`delete the ${kind} for ${who(l)}`, `logins/${l.id}/delete`);
    const extra = kind === "passkey" ? " · passkey" : "";
    const line = `${l.username || "(no username)"}${extra}`;
    const used = l.used ? ` · used ${l.used}` : ""; // not on a phone
    const open = el(
      "button",
      { type: "button", className: "who" },
      el("strong", { textContent: l.site }),
      el("span", { title: line + used }, line, el("span", { className: "used", textContent: used }))
    );
    open.setAttribute("aria-label", `Edit the ${kind} for ${who(l)}`);
    open.onclick = () => editLogin(l);
    const more = el("span", { className: "more", innerHTML: FORWARD });
    list.append(el("li", {}, open, ask, remove, more));
  }
}

// Making a passkey: only while you have the browser, and the site makes it on your click.
// While the view waits for one, a card under the screen says so too, with the same button.
function showMaking(s) {
  const yours = mine();
  $("make").hidden = !yours;
  $("make").textContent = s.making ? "Stop waiting" : "Make a passkey";
  $("make").onclick = $("stop").onclick = () => act("make a passkey", "passkeys/make", { on: !s.making });
  $("making").hidden = !(yours && s.making);
  $("make-note").textContent = !yours
    ? s.made || "Take over to make a passkey."
    : s.making
      ? "Add one on the site's page."
      : s.made || "";
}

async function refresh() {
  try {
    const r = await fetch(base + "state" + query, { cache: "no-store" });
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
  $("caption").hidden = !$("caption").textContent;
  showTalk(s);
}

async function post(what, body) {
  let r;
  try {
    r = await fetch(base + what + query, {
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
  if ((what === "take" || what === "open") && !$("typing").contains(document.activeElement)) rfb?.focus();
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

// --- the message bar, in the app ---

let talking = false; // the bar is open
let wasMine = false; // the tab was yours as the bar was last drawn
let held = []; // messages sent while a request waited, to go once it's answered
let said = ""; // how the last message went, as the app says it

function showTalk(s) {
  if (!inApp) return;
  const yours = mine();
  const asked = waits(s);
  const ended = gone || !!closedTab(s);
  if (yours && !wasMine) talking = true; // taking the tab opens it
  wasMine = yours;
  if (asked) talking = true;
  if (ended) talking = false;
  if (!asked && held.length) {
    const going = held;
    held = [];
    for (const text of going) say(text);
    if (yours) give();
  }
  $("talk").hidden = !talking;
  $("fab").hidden = talking || ended;
  $("caption").hidden = talking || !$("caption").textContent;
  const empty = !$("say").value.trim();
  $("talk-give").hidden = !(yours && !asked && empty);
  $("talk-send").hidden = !$("talk-give").hidden;
  $("talk-send").setAttribute("aria-label", yours && !asked ? "Send and hand back" : "Send to the agent");
  $("talk-close").hidden = asked;
  $("say").placeholder = yours ? "Message the agent…" : "Tell the agent…";
  $("say").setAttribute(
    "aria-label",
    yours ? "Message to the agent. Optional. Sending hands the tab back" : "Message to the agent"
  );
  $("said").textContent = held.length
    ? held.length > 1
      ? `${held.length} messages held until you answer`
      : `Held until you answer: ${held[0]}`
    : said;
}

// A message for the agent, into the chat through the app.
function say(text) {
  said = `Sent: ${text}`;
  app.postMessage(JSON.stringify({ say: text }));
}

async function give() {
  talking = false;
  await act("hand back", "give");
  showTalk(shownState);
}

if (inApp) {
  window.anywhereLLM = {
    // How the app took the last message, in its words.
    said(line) {
      said = String(line || "");
      showTalk(shownState);
    },
  };
  $("back").hidden = false;
  $("back").addEventListener("click", () => app.postMessage(JSON.stringify({ back: true })));
  $("fab").addEventListener("click", () => {
    talking = true;
    showTalk(shownState);
    $("say").focus(); // in the tap's own handler, so a phone opens its keyboard
  });
  $("talk-close").addEventListener("click", () => {
    talking = false;
    showTalk(shownState);
  });
  $("say").addEventListener("input", () => showTalk(shownState));
  $("talk").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("say").value.trim();
    if (waits(shownState)) {
      // Typed text never answers a request: it waits for the answer.
      if (!text) return;
      held.push(text);
      $("say").value = "";
      showTalk(shownState);
      return;
    }
    if (!text && !mine()) return;
    $("say").value = "";
    if (text) say(text);
    if (mine()) give();
    else showTalk(shownState);
  });
}
document.body.classList.toggle("in-app", inApp);

// --- the address ---

function editAddress() {
  const t = shown(shownState);
  if (!t || gone || closedTab(shownState)) return;
  $("address-field").value = t.url || "";
  $("title").hidden = true;
  $("address").hidden = false;
  $("address-field").focus();
  $("address-field").select();
}

function leaveAddress() {
  $("address").hidden = true;
  $("title").hidden = false;
}

$("address-open").addEventListener("click", editAddress);
$("address").addEventListener("submit", (e) => {
  e.preventDefault();
  const url = $("address-field").value.trim();
  const t = shown(shownState);
  leaveAddress();
  if (url) act("open the page", "open", { url, tab: t?.id || tab });
});
$("address-field").addEventListener("keydown", (e) => e.key === "Escape" && leaveAddress());
// Leaving the field keeps the tab where it was.
$("address-field").addEventListener("blur", () => setTimeout(leaveAddress, 0));

$("ended").lastElementChild.addEventListener("click", () => {
  const closed = closedTab(shownState);
  if (gone) location.reload();
  else if (closed) act("open the tab again", "open", { tab: closed.id });
});

$("take").addEventListener("click", () => {
  if (TOUCH.matches) keyboard();
  act("take over", "take");
});
// On a phone there's no button: touching the page takes it.
$("touch").addEventListener("click", () => act("take over", "take"));
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

// The buttons sit in the header, or in a bar at the foot on a phone; in the app, the
// keyboard and fit buttons go in the message bar, which hands back.
function place() {
  if (inApp && NARROW.matches) {
    $("tools").append($("keyboard"), $("fit"));
    return;
  }
  $("actions").prepend($("keyboard"), $("fit"));
  if (NARROW.matches) $("row").append($("actions"));
  else $("logins-open").after($("actions"));
}
NARROW.addEventListener("change", () => {
  place();
  size();
});

// --- the saved logins ---

// The saved logins, a panel opened from the bar, showing their list, the form that adds one
// or the one that changes one; the focus goes with it.
function logins(open, view = "list") {
  $("logins").hidden = $("scrim").hidden = !open;
  $("logins-open").setAttribute("aria-expanded", String(open));
  $("logins-list").hidden = view !== "list";
  $("add").hidden = view !== "add";
  $("edit").hidden = view !== "edit";
  if (!open) $("logins-open").focus();
  else if (view === "add") $("add").site.focus();
  else if (view === "edit") $("edit").querySelector(".back").focus();
  else $("logins-list").querySelector(".close").focus();
}

let editing = null; // the login the edit form changes

// A login's site, username and password to change, or to remove; a passkey's are the
// site's own, so it can only be removed.
function editLogin(l) {
  editing = l;
  const f = $("edit");
  const passkey = l.kind === "passkey";
  $("edit-title").textContent = l.site;
  f.site.value = l.site;
  f.username.value = l.username || "";
  f.password.value = "";
  f.site.disabled = f.username.disabled = passkey;
  $("edit-password").hidden = passkey;
  $("edit-save").hidden = passkey;
  $("edit-note").textContent = passkey
    ? "Signs in with a passkey, so there is no password to change."
    : "The saved password is never shown.";
  sure(false);
  $("edit-error").textContent = "";
  logins(true, "edit");
}

function sure(asking) {
  $("edit-sure").hidden = !asking;
  $("edit-buttons").hidden = asking;
  if (asking) $("edit-sure").firstElementChild.textContent = `Remove the ${editing.kind} for ${who(editing)}?`;
}

$("logins-open").addEventListener("click", () => logins($("logins").hidden));
for (const close of $("logins").querySelectorAll(".close")) close.addEventListener("click", () => logins(false));
$("add-open").addEventListener("click", () => logins(true, "add"));
$("add-back").addEventListener("click", () => logins(true));
$("edit-back").addEventListener("click", () => logins(true));
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
$("edit").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const error = await post(`logins/${editing.id}`, {
    site: f.site.value, username: f.username.value, password: f.password.value,
  });
  $("edit-error").textContent = error || "";
  if (!error) logins(true);
});
$("edit-remove").addEventListener("click", () => sure(true));
$("edit-keep").addEventListener("click", () => sure(false));
$("edit-delete").addEventListener("click", async () => {
  const error = await post(`logins/${editing.id}/delete`);
  $("edit-error").textContent = error || "";
  if (!error) logins(true);
});

place();
connect();
refresh();
// The state every 2 s while the page is in view, and as soon as it's back in view.
const visible = () => document.visibilityState !== "hidden";
setInterval(() => visible() && refresh(), 2000);
document.addEventListener("visibilitychange", () => visible() && refresh());
