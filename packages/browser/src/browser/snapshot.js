// The page as the agent reads it (browser.driver): its visible interactive elements, each
// tagged with a ref (data-bw-ref) that browser-act targets, then its visible text. A ref
// stays with its element for the life of the page, so refs from an earlier read still
// work while the element is there. Open shadow roots are walked; frames aren't.
(maxElements) => {
  const INTERACTIVE = new Set(["A", "BUTTON", "INPUT", "SELECT", "TEXTAREA", "SUMMARY"]);
  const ROLES = new Set([
    "button", "link", "checkbox", "radio", "switch", "tab", "menuitem", "menuitemcheckbox",
    "menuitemradio", "option", "combobox", "textbox", "searchbox", "slider", "spinbutton",
  ]);
  const clip = (s, n) => {
    s = (s || "").replace(/\s+/g, " ").trim();
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const st = getComputedStyle(el);
    return st.visibility !== "hidden" && st.display !== "none" && st.opacity !== "0";
  };
  const interactive = (el) => {
    if (INTERACTIVE.has(el.tagName)) {
      if (el.tagName === "A") return el.hasAttribute("href");
      if (el.tagName === "INPUT") return el.type !== "hidden";
      return true;
    }
    const role = el.getAttribute("role");
    return (role && ROLES.has(role)) || (el.isContentEditable && el.parentElement?.isContentEditable !== true);
  };
  const name = (el) =>
    clip(
      el.getAttribute("aria-label") ||
        (el.labels && el.labels[0] && el.labels[0].innerText) ||
        el.getAttribute("title") ||
        el.getAttribute("alt") ||
        (["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName) ? "" : el.innerText) ||
        el.getAttribute("placeholder") ||
        el.getAttribute("name") ||
        "",
      80
    );
  const describe = (el) => {
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute("role");
    let kind = role || (tag === "a" ? "link" : tag === "summary" ? "button" : tag);
    if (tag === "input") kind = `input[${el.type}]`;
    let line = `${kind} "${name(el)}"`;
    if (tag === "a") {
      const href = el.getAttribute("href") || "";
      if (href && !href.startsWith("javascript:")) line += ` -> ${clip(href, 100)}`;
    }
    if (tag === "input" && (el.type === "checkbox" || el.type === "radio")) {
      line += el.checked ? " (checked)" : " (unchecked)";
    } else if (tag === "input" && el.type === "password") {
      if (el.value) line += " (filled)";
    } else if (tag === "input" || tag === "textarea") {
      if (el.value) line += ` value="${clip(el.value, 80)}"`;
      else if (el.placeholder) line += ` placeholder="${clip(el.placeholder, 60)}"`;
    } else if (tag === "select") {
      const options = [...el.options].slice(0, 12).map((o) => (o.selected ? `*${clip(o.text, 40)}` : clip(o.text, 40)));
      line += ` options: ${options.join(" | ")}${el.options.length > 12 ? " | …" : ""}`;
    }
    if (el.disabled || el.getAttribute("aria-disabled") === "true") line += " (disabled)";
    return line;
  };

  let next = window.__bwNext || 1;
  const lines = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot) walk(el.shadowRoot);
      if (lines.length >= maxElements || !interactive(el) || !visible(el)) continue;
      let ref = el.getAttribute("data-bw-ref");
      if (!ref) {
        ref = `e${next++}`;
        el.setAttribute("data-bw-ref", ref);
      }
      lines.push(`[${ref}] ${describe(el)}`);
    }
  };
  walk(document);
  window.__bwNext = next;
  const text = (document.body ? document.body.innerText : "")
    .split("\n")
    .map((l) => l.replace(/\s+/g, " ").trim())
    .filter((l, i, all) => l || (i > 0 && all[i - 1]))
    .join("\n")
    .trim();
  return { elements: lines, text, more: lines.length >= maxElements };
}
