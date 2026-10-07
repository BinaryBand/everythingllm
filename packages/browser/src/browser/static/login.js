// The form for a login the agent asked for (browser.takeover, /login/<id>/): sends it to the
// runner, which saves it in the workspace's vault, or turns the request down. Nothing comes
// back but how the request stands, and that goes in as text.

const base = location.pathname.replace(/[^/]*$/, ""); // /login/<id>/
const $ = (id) => document.getElementById(id);

function show(state) {
  if (state.state === "waiting") return;
  $("ask-form").hidden = true;
  $("result").textContent = state.message;
  $("result").hidden = false;
}

async function post(what, body) {
  $("error").hidden = true;
  let r;
  try {
    r = await fetch(base + what, {
      method: "POST",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    return problem("Couldn't reach the browser runner.");
  }
  const raw = await r.text().catch(() => "");
  let data = {};
  try {
    data = JSON.parse(raw);
  } catch {}
  if (!r.ok) return problem(data.error || raw.trim() || `${r.status}`);
  show(data);
}

function problem(text) {
  $("error").textContent = text;
  $("error").hidden = false;
}

$("ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  post("save", {
    site: f.site.value, username: f.username.value, password: f.password.value,
    totp: f.totp.value, ask: f.ask.checked,
  }).then(() => {
    if ($("ask-form").hidden) f.reset(); // saved: don't keep the password in the page
  });
});
$("decline").addEventListener("click", () => post("drop"));
