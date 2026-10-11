// The form for a login the agent asked for (browser.takeover, /login/<id>/): sends it to the
// runner, which saves it in the workspace's vault, or turns the request down. Nothing comes
// back but how the request stands; once it's answered the page is drawn again by the runner,
// which says how it ended.

const base = location.pathname.replace(/[^/]*$/, ""); // /login/<id>/
const $ = (id) => document.getElementById(id);

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
  if (data.state === "waiting") return;
  $("ask-form").reset(); // so the reload keeps no password in the page
  location.reload();
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
    totp: f.totp.value, ask: f.ask.value === "yes",
  });
});
$("decline").addEventListener("click", () => post("drop"));
$("add-totp").addEventListener("click", () => {
  $("totp").hidden = false;
  $("add-totp").remove();
  $("ask-form").totp.focus();
});
