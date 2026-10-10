"""Apps: a page the agent writes, plus data the host keeps for it and the page saves to.

An app is a workspace's folder /project/apps/<name>/ (docs/.proposals/sandbox-apps.md):

  page.html   the agent's page, with PLACEHOLDER once inside a <script>, where the host puts
              the app ({title, version, card, data, token, ops, sync}) as JSON when it
              renders the page to /public/apps/<name>/index.html, beside CLIENT as app.js
  data.json   {"title", "version", "card", "data"}: `data` is the page's, any JSON; `card`
              is what the chat's card shows until a picture of the page is taken, or when
              one can't be (card.py, sandbox.appshot): {"summary"?, "lines": [text]} or
              {"summary"?, "items": "/<a list in the data>", "text"?: key, "done"?: key,
              "where"?: {key: value}}, rows with a checkbox read from the data (those
              whose keys have those values), so the card can't drift from it

The page and the agent change the app the same way, with a JSON patch (patch.py) on its
title (/title), card (/card) and data (any other path, /items/0 for /data/items/0); the host checks the result here, saves it one version on and renders
the page again (sandbox.runner's op_app, sandbox.appsweb's write-back). Nothing here knows
what kind of app it is: a starter (STARTERS, a page in starters/) is only a page to begin
from, copied into the workspace, which the agent may change like any of its own.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from sandbox.apps import card as cards
from sandbox.apps import patch
from sandbox.apps.base import AppError, text

__all__ = [
    "CLIENT",
    "PLACEHOLDER",
    "STARTERS",
    "AppError",
    "card",
    "embed",
    "migrate",
    "new",
    "render",
    "starter_page",
    "summary",
    "validate",
]

PLACEHOLDER = "/*APP*/null"
CLIENT = Path(__file__).with_name("client.js")
MAX_TITLE = 120
MAX_LINE = 200
MAX_KEY = 64
# A starter's first data and card; its page is starters/<name>.html.
STARTERS: dict[str, dict[str, Any]] = {
    "list": {"data": {"items": []}, "card": {"items": "/data/items"}},
    "tracker": {
        "data": {"habits": []},
        "card": {"items": "/data/habits", "text": "name"},
    },
    "pomodoro": {
        "data": {
            "focus": 25,
            "rest": 5,
            "phase": "focus",
            "endsAt": None,
            "left": 1500,
            "log": [],
        },
        "card": {
            "summary": "Focus",
            "lines": ["25 minutes, ready", "0 focus sessions today"],
        },
    },
    "blank": {"data": {}, "card": {}},
}


def starter(name: str) -> dict[str, Any]:
    if name not in STARTERS:
        raise AppError(f"starter must be one of: {', '.join(STARTERS)}")
    return STARTERS[name]


def new(title: str, name: str = "") -> dict[str, Any]:
    """An app's first data, empty or a starter's."""
    first = starter(name) if name else {"data": {}, "card": {}}
    return validate(
        {
            "title": title,
            "version": 0,
            "card": first["card"],
            "data": copy.deepcopy(first["data"]),
        }
    )


def starter_page(name: str) -> bytes:
    starter(name)
    return Path(__file__).with_name("starters").joinpath(f"{name}.html").read_bytes()


def valid_card(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AppError("the card must be an object")
    out: dict[str, Any] = {}
    if value.get("summary") not in (None, ""):
        out["summary"] = text(value["summary"], "the card's summary", MAX_TITLE)
    if value.get("lines") is not None:
        lines = value["lines"]
        if not isinstance(lines, list) or len(lines) > cards.ROWS:
            raise AppError(f"the card's lines must be a list of at most {cards.ROWS}")
        out["lines"] = [text(line, "a card line", MAX_LINE) for line in lines]
    if value.get("items") is not None:
        tokens = patch.pointer(value["items"], "the card's items")
        if tokens[0] != "data" or len(tokens) < 2:
            raise AppError("the card's items must point to a list in the data")
        out["items"] = patch.spelled(tokens)
        for key in ("text", "done"):
            if value.get(key) is not None:
                out[key] = text(value[key], f"the card's {key}", MAX_KEY)
        where = value.get("where")
        if where is not None:
            if (
                not isinstance(where, dict)
                or len(where) > 4
                or not all(
                    isinstance(v, (str, int, float, bool)) or v is None
                    for v in where.values()
                )
            ):
                raise AppError(
                    "the card's where must be at most 4 keys and plain values, "
                    'e.g. {"status": "reading"}'
                )
            out["where"] = {
                text(k, "a key in the card's where", MAX_KEY): v
                for k, v in where.items()
            }
    return out


def validate(app: Any) -> dict[str, Any]:
    """The app's data, checked and normalised (AppError when it isn't usable; a run or a
    patch may have changed it). `data` is any JSON and is kept as it is."""
    if not isinstance(app, dict):
        raise AppError("the app's data isn't a JSON object")
    if "data" not in app:
        raise AppError("the app's data has no 'data'")
    if isinstance(app["data"], dict) and (taken := set(patch.OWNED) & set(app["data"])):
        raise AppError(
            f"the data can't have a top-level {', '.join(sorted(taken))}: /title and "
            "/card are the app's own; use another name"
        )
    version = app.get("version")
    whole = isinstance(version, int) and not isinstance(version, bool)
    return {
        "title": text(app.get("title"), "the title", MAX_TITLE),
        "version": version if whole and version >= 0 else 0,
        "card": valid_card(app.get("card")),
        "data": app["data"],
    }


def migrate(old: Any) -> dict[str, Any] | None:
    """An app from the time of repo templates (a list's data) in today's shape, or None
    when it isn't one. Its page is the list starter."""
    if not isinstance(old, dict) or old.get("template") != "list":
        return None
    items = old.get("items") if isinstance(old.get("items"), list) else []
    return validate(
        {
            "title": old.get("title") or "List",
            "version": old.get("version"),
            "card": STARTERS["list"]["card"],
            "data": {
                "items": [
                    {"text": i.get("text", ""), "done": i.get("done") is True}
                    for i in items
                    if isinstance(i, dict)
                ]
            },
        }
    )


def rows(app: dict[str, Any]) -> list[tuple[str, bool | None]]:
    """What the card shows, as (text, done); done None for a plain line."""
    c = app["card"]
    if "items" in c:
        try:
            found = patch.get(app, c["items"])
        except AppError:
            return []
        if not isinstance(found, list):
            return []
        key, done = c.get("text", "text"), c.get("done", "done")
        where = c.get("where", {})
        out = []
        for item in found:
            if where and not (
                isinstance(item, dict)
                and all(item.get(k) == v for k, v in where.items())
            ):
                continue
            if isinstance(item, dict):  # a checkbox only for an item with a done key
                ticked = item[done] is True if done in item else None
                value = item.get(key)
                out.append(("" if value is None else str(value), ticked))
            elif isinstance(item, str):
                out.append((item, None))
        return out
    return [(line, None) for line in c.get("lines", [])]


def summary(app: dict[str, Any]) -> str:
    """A line about the app now: the card's summary, or a count of its rows (of those
    with a checkbox left to tick, when there are any)."""
    if app["card"].get("summary"):
        return app["card"]["summary"]
    if "items" not in app["card"]:
        return ""
    found = rows(app)
    if not found:
        return "empty"
    if all(done is None for _, done in found):
        return f"{len(found)} item{'s' if len(found) != 1 else ''}"
    ticked = [done for _, done in found if done is not None]
    left = ticked.count(False)
    return f"{left} of {len(ticked)} left" if left else f"all {len(ticked)} done"


def card(app: dict[str, Any], theme: str) -> bytes:
    """The app's card, a PNG."""
    return cards.draw(app["title"], summary(app), rows(app), theme)


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


def render(page: str, app: dict[str, Any], token: str, ops: str, sync: str) -> str:
    """The app's page with the app, its write-back token and where to post in place of
    PLACEHOLDER."""
    if page.count(PLACEHOLDER) != 1:
        raise AppError(
            f"page.html must have {PLACEHOLDER} exactly once, inside a <script> "
            f"(e.g. App.start({PLACEHOLDER}))"
        )
    return page.replace(
        PLACEHOLDER, embed({**app, "token": token, "ops": ops, "sync": sync})
    )
