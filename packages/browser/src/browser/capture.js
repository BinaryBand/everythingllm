// In every page of a workspace's browser (browser.driver adds it as an init script): when a
// form with a filled password is sent, hand its username and password to the driver, which
// keeps them as an offer to save only while the user has the browser, and only for the site
// of the frame they came from. The take-over view then asks whether to save the login.
(() => {
  if (window.__bwCaptureReady) return;
  window.__bwCaptureReady = true;
  const USERLIKE = new Set(["text", "email", "tel", ""]);
  const grab = (scope) => {
    const root = scope || document;
    const password = [...root.querySelectorAll("input[type=password]")].find((i) => i.value);
    if (!password || typeof window.__bwCapture !== "function") return;
    const inputs = [...(password.form || document).querySelectorAll("input")];
    const at = inputs.indexOf(password); // -1 for a field outside the form it belongs to
    const user = inputs
      .slice(0, Math.max(at, 0))
      .reverse()
      .find((i) => USERLIKE.has(i.type) && i.value);
    window.__bwCapture({ username: user ? user.value : "", password: password.value });
  };
  document.addEventListener("submit", (e) => grab(e.target), true);
  document.addEventListener(
    "keydown",
    (e) => {
      if (e.key === "Enter" && e.target && e.target.type === "password") grab(e.target.form);
    },
    true
  );
  document.addEventListener(
    "click",
    (e) => {
      const button = e.target && e.target.closest && e.target.closest("button, input[type=submit], [role=button]");
      if (button) grab(button.form || null);
    },
    true
  );
})();
