"""What the sites' templates may not do; a test holds every template in the repo to it.

The pages site's CSP already stops scripts, inline styles and fetches from other hosts in a
browser, and zola builds without a network (sites.build). These checks catch the rest:
Zola functions that read files from the host or the environment while the site builds, and
markup that would post, frame or redirect somewhere. `| safe` is kept to the content of a
page or section (Markdown that Zola rendered and the entry store escaped): on anything else
it would turn an entry's `extra` fields, which the agent writes directly, into raw HTML.
"""

import re

FUNCTIONS = re.compile(
    r"\b(load_data|get_env|get_hash|get_image_metadata|resize_image)\s*\("
)
TAGS = re.compile(
    r"<\s*(script|iframe|frame|frameset|object|embed|applet|form|base)\b", re.IGNORECASE
)
META_REFRESH = re.compile(r"<\s*meta\b[^>]*\bhttp-equiv\b", re.IGNORECASE)
HANDLER = re.compile(r"[\s\"'/]on[a-z]+\s*=", re.IGNORECASE)
STYLE_ATTR = re.compile(r"[\s\"'/]style\s*=", re.IGNORECASE)
JS_URL = re.compile(r"javascript\s*:", re.IGNORECASE)
FILTER_BLOCK = re.compile(r"\{%-?\s*(filter|autoescape)\b")
SAFE = re.compile(r"\|\s*safe\b")
SAFE_OK = re.compile(r"\{\{-?\s*(page|section)\.content\s*\|\s*safe\s*-?\}\}")


def problems(template: str) -> list[str]:
    """What's wrong with a template; empty when nothing is."""
    found = []
    if m := FUNCTIONS.search(template):
        found.append(
            f"{m.group(1)}() reads from the host while the site builds; it isn't allowed"
        )
    if m := TAGS.search(template):
        found.append(
            f"<{m.group(1).lower()}> isn't allowed (pages run no scripts and post or frame nothing)"
        )
    if META_REFRESH.search(template):
        found.append("<meta http-equiv> isn't allowed (it can redirect the page)")
    if HANDLER.search(template) or JS_URL.search(template):
        found.append(
            "event handlers (on...=) and javascript: URLs aren't allowed; pages run no scripts"
        )
    if STYLE_ATTR.search(template):
        found.append(
            "style= attributes are blocked by the CSP; put the rules in the site's stylesheet"
        )
    if m := FILTER_BLOCK.search(template):
        found.append(f"{{% {m.group(1)} %}} blocks aren't allowed")
    if len(SAFE.findall(template)) > len(SAFE_OK.findall(template)):
        found.append(
            "`| safe` is only allowed as {{ page.content | safe }} or {{ section.content | safe }}"
        )
    return found
