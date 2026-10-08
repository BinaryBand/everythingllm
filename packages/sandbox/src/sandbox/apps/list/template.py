"""The list template: items to tick off (shopping, packing, to-dos).

Data: {"template": "list", "title", "items": [{"id", "text", "done"}], "next_id", "version"}.
An item is named in an op by its id (the page's way) or by its text, as the agent says it
("oat milk"), matched without regard to case.
"""

from __future__ import annotations

import io
from typing import Any

from chatimage import BAR, PAD, THEMES, WIDTH, clean, fit, font, frame
from PIL import Image, ImageDraw

from sandbox.apps.base import AppError, text

NAME = "list"
LABEL = "List"
MAX_ITEMS = 200
MAX_TEXT = 200
MAX_TITLE = 120
CARD_ROWS = 6
OPS = ("add", "check", "uncheck", "remove", "clear_done", "rename")


def new(title: str) -> dict[str, Any]:
    return {
        "template": NAME,
        "title": text(title, "the title", MAX_TITLE),
        "items": [],
        "next_id": 1,
        "version": 0,
    }


def whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("template") != NAME:
        raise AppError("the app's data isn't a list's")
    items = data.get("items")
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise AppError(f"the list's items aren't a list of at most {MAX_ITEMS}")
    out, ids = [], set()
    for item in items:
        if not isinstance(item, dict) or not whole(item.get("id")) or item["id"] in ids:
            raise AppError("an item has no id of its own")
        ids.add(item["id"])
        out.append(
            {
                "id": item["id"],
                "text": text(item.get("text"), "an item", MAX_TEXT),
                "done": item.get("done") is True,
            }
        )
    next_id = data.get("next_id")
    version = data.get("version")
    return {
        "template": NAME,
        "title": text(data.get("title"), "the title", MAX_TITLE),
        "items": out,
        "next_id": max([next_id if whole(next_id) else 1, *(i + 1 for i in ids)]),
        "version": version if whole(version) and version >= 0 else 0,
    }


def find(data: dict, which: Any, open_first: bool) -> dict:
    """The item `which` names: an id, or text matched without case (an open one first
    when `open_first`, else a done one)."""
    if whole(which):
        for item in data["items"]:
            if item["id"] == which:
                return item
        raise AppError(f"there's no item {which} on the list")
    wanted = text(which, "the item", MAX_TEXT).casefold()
    matches = [i for i in data["items"] if i["text"].casefold() == wanted]
    if not matches:
        raise AppError(f"there's no '{which}' on the list")
    matches.sort(key=lambda i: i["done"] == open_first)
    return matches[0]


def apply(data: dict, op: str, args: Any) -> tuple[dict, str]:
    """The list after `op`, and what happened, in a few words."""
    if op not in OPS:
        raise AppError(f"op must be one of: {', '.join(OPS)}")
    args = args if isinstance(args, dict) else {}
    data = {**data, "items": [dict(i) for i in data["items"]]}
    if op == "add":
        given = args.get("items")
        if given is None and args.get("item") is not None:
            given = [args["item"]]
        if not isinstance(given, list) or not given:
            raise AppError("add takes item (text) or items (a list of text)")
        names = [text(g, "an item", MAX_TEXT) for g in given]
        open_now = {i["text"].casefold() for i in data["items"] if not i["done"]}
        added = []
        for name in names:
            if name.casefold() in open_now:
                continue  # already on the list, not ticked off
            if len(data["items"]) >= MAX_ITEMS:
                raise AppError(f"the list is full ({MAX_ITEMS} items)")
            data["items"].append({"id": data["next_id"], "text": name, "done": False})
            data["next_id"] += 1
            open_now.add(name.casefold())
            added.append(name)
        return data, (
            f"added {', '.join(added)}" if added else "it was on the list already"
        )
    if op in ("check", "uncheck", "remove"):
        item = find(data, args.get("item"), open_first=op != "uncheck")
        if op == "remove":
            data["items"] = [i for i in data["items"] if i["id"] != item["id"]]
            return data, f"removed {item['text']}"
        item["done"] = op == "check"
        return data, f"{'ticked off' if item['done'] else 'unticked'} {item['text']}"
    if op == "clear_done":
        gone = sum(i["done"] for i in data["items"])
        data["items"] = [i for i in data["items"] if not i["done"]]
        return data, f"cleared {gone} done"
    data["title"] = text(args.get("title"), "the title", MAX_TITLE)
    return data, f"renamed it {data['title']}"


def summary(data: dict) -> str:
    left = sum(not i["done"] for i in data["items"])
    total = len(data["items"])
    if not total:
        return "empty"
    return f"{left} of {total} left" if left else f"all {total} done"


def card(data: dict, theme: str) -> bytes:
    """The list as the chat's card: open items first, then done ones struck through, at
    most CARD_ROWS rows and a count of the rest."""
    p = THEMES[theme]
    accent = p.app
    rows = [i for i in data["items"] if not i["done"]] + [
        i for i in data["items"] if i["done"]
    ]
    shown, rest = rows[:CARD_ROWS], len(rows) - CARD_ROWS
    small, body, big = font("regular", 32), font("regular", 40), font("bold", 58)
    row_h, row_gap = 48, 18
    height = 52 + 38 + 24 + 70 + 28 + 46  # label, title, footer
    height += max(1, len(shown)) * (row_h + row_gap) + (row_h if rest > 0 else 0) + 40
    image = Image.new("RGBA", (WIDTH, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    frame(d, height, accent, p)
    x, width = BAR + PAD, WIDTH - BAR - 2 * PAD
    y = 52
    d.text(
        (x, y),
        fit(d, f"{LABEL} · {summary(data)}", small, width),
        font=small,
        fill=accent,
    )
    y += 38 + 24
    d.text((x, y), fit(d, clean(data["title"]), big, width), font=big, fill=p.title)
    y += 70 + 28
    if not shown:
        d.text((x, y), "Nothing on the list yet.", font=body, fill=p.faint)
        y += row_h + row_gap
    for item in shown:
        box = (x, y + 2, x + 44, y + 46)
        if item["done"]:
            d.rounded_rectangle(box, 10, fill=accent)
            d.line(
                [(x + 10, y + 25), (x + 19, y + 34), (x + 35, y + 14)],
                fill=p.panel,
                width=6,
                joint="curve",
            )
        else:
            d.rounded_rectangle(box, 10, outline=p.faint, width=4)
        label = fit(d, clean(item["text"]), body, width - 72)
        tx = x + 72
        d.text((tx, y), label, font=body, fill=p.faint if item["done"] else p.title)
        if item["done"]:
            mid = y + 26
            d.line(
                [(tx, mid), (tx + d.textlength(label, font=body), mid)],
                fill=p.faint,
                width=3,
            )
        y += row_h + row_gap
    if rest > 0:
        d.text((x, y), f"+ {rest} more", font=small, fill=p.faint)
        y += row_h
    d.text((x, height - 46 - 32), "→ tap to open", font=small, fill=p.faint)
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue()
