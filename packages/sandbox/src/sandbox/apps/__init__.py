"""App templates: the kinds of app the agent makes by writing data, not pages.

An app is a template from here plus a workspace's data for it (docs/.proposals/sandbox-apps.md).
The agent creates and changes one with the `app` skill (runner.Runner.op_app), and the page
it opens writes back through the apps server (sandbox.appsweb); either way the change goes
through the template's own ops, and the host renders the page from the template with the
data embedded as JSON, so no HTML is ever built from what the data says. The templates are
the repo's, reviewed with the runner that renders them: a workspace can't change one.

A template is a package beside this file with:

  NAME, LABEL      its name in the skill, and in the card's label
  new(title)       an empty instance's data
  validate(data)   the data, checked and normalised (AppError when it isn't usable;
                   a run may have edited the file)
  OPS, apply(data, op, args) -> (data, what happened)   its changes
  summary(data)    a line about it now, for the agent and the card
  card(data, theme) -> PNG bytes   the live card's frame (chatimage's look)
  page.html        the page, one file, CSS and JS inline, `/*APP*/null` where the data goes
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Any

from sandbox.apps.base import AppError
from sandbox.apps.list import template as list_template

__all__ = ["TEMPLATES", "AppError", "embed", "of", "render", "template"]

TEMPLATES: dict[str, ModuleType] = {list_template.NAME: list_template}
PLACEHOLDER = "/*APP*/null"


def template(name: Any) -> ModuleType:
    if not isinstance(name, str) or name not in TEMPLATES:
        raise AppError(f"template must be one of: {', '.join(TEMPLATES)}")
    return TEMPLATES[name]


def of(data: Any) -> ModuleType:
    """The template an instance's data names."""
    if not isinstance(data, dict):
        raise AppError("the app's data isn't a JSON object")
    return template(data.get("template"))


def embed(value: Any) -> str:
    """`value` as JSON that can sit inside a <script> without ending it or starting markup."""
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def render(data: dict, token: str, ops_url: str) -> str:
    """The app's page: its template's page.html with the data, the write-back token and
    the address to post ops to in place of PLACEHOLDER."""
    page = (Path(of(data).__file__).with_name("page.html")).read_text()
    if page.count(PLACEHOLDER) != 1:
        raise AppError("the template's page has no single place for its data")
    return page.replace(
        PLACEHOLDER, embed({"data": data, "token": token, "ops": ops_url})
    )
