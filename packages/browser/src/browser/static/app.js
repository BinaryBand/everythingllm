// The take-over view's page (browser.takeover): noVNC showing the workspace's browser,
// view-only while the agent has it, and the buttons that take it and hand it back.
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

async function refresh() {
  try {
    const r = await fetch(base + "state", { cache: "no-store" });
    if (r.ok) show(await r.json());
  } catch {}
}

async function post(what) {
  const r = await fetch(base + what, { method: "POST" });
  if (r.ok) show(await r.json());
  if (what === "take") rfb?.focus();
}

$("take").addEventListener("click", () => post("take"));
$("give").addEventListener("click", () => post("give"));
connect();
refresh();
setInterval(refresh, 2000);
