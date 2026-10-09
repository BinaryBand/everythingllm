"""JSON Patch (RFC 6902) for an app's data, the one way its page and the agent change it.

The operations are add, remove, replace, move, copy and test, applied in order to a copy of
the app, all or none (AppError names the first that fails). A path may reach only the parts
of an app its page owns: /title, /card and /data, never /version or the app as a whole. Any
other path is taken to be in the data (/items/0 is /data/items/0), which is how a page
thinks of its own data.
"""

from __future__ import annotations

import copy
from typing import Any

from sandbox.apps.base import AppError

OPS = ("add", "remove", "replace", "move", "copy", "test")
OWNED = ("title", "card", "data")
MAX_OPS = 1000


def pointer(path: Any, what: str = "path") -> list[str]:
    """A JSON pointer's tokens, from a part of the app the page owns: /title, /card or
    /data, and anything else in /data."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise AppError(f"{what} must be a JSON pointer like /items/0")
    tokens = [t.replace("~1", "/").replace("~0", "~") for t in path[1:].split("/")]
    return tokens if tokens[0] in OWNED else ["data", *tokens]


def spelled(tokens: list[str]) -> str:
    """Tokens as a pointer again, in full (/data/...)."""
    return "".join("/" + t.replace("~", "~0").replace("/", "~1") for t in tokens)


def index(container: list, token: str, path: str, appending: bool) -> int:
    if appending and token == "-":
        return len(container)
    if not token.isdigit() or (token != "0" and token.startswith("0")):
        raise AppError(f"{path}: {token!r} isn't an index")
    i = int(token)
    if i > len(container) or (i == len(container) and not appending):
        raise AppError(f"{path}: there's no item {i}")
    return i


def parent(doc: Any, tokens: list[str], path: str) -> Any:
    """What the pointer's last token is in."""
    here = doc
    for token in tokens[:-1]:
        if isinstance(here, dict) and token in here:
            here = here[token]
        elif isinstance(here, list):
            here = here[index(here, token, path, False)]
        else:
            raise AppError(f"{path}: there's nothing at {token!r}")
    return here


def get(doc: Any, path: str) -> Any:
    tokens = pointer(path)
    where, last = parent(doc, tokens, path), tokens[-1]
    if isinstance(where, dict) and last in where:
        return where[last]
    if isinstance(where, list):
        return where[index(where, last, path, False)]
    raise AppError(f"{path}: there's nothing there")


def remove(doc: Any, path: str) -> Any:
    tokens = pointer(path)
    if len(tokens) == 1:
        raise AppError(f"{path} can't be removed")
    where, last = parent(doc, tokens, path), tokens[-1]
    if isinstance(where, dict) and last in where:
        return where.pop(last)
    if isinstance(where, list):
        return where.pop(index(where, last, path, False))
    raise AppError(f"{path}: there's nothing there")


def add(doc: Any, path: str, value: Any) -> None:
    tokens = pointer(path)
    where, last = parent(doc, tokens, path), tokens[-1]
    if isinstance(where, dict):
        where[last] = value
    elif isinstance(where, list):
        where.insert(index(where, last, path, True), value)
    else:
        raise AppError(f"{path}: its parent isn't an object or a list")


def unguarded(patch: list) -> list[str]:
    """The paths in `patch` that change a list's item by its place (remove, replace, move or
    copy from /items/3, or in it) with no `test` before it of that item or in it: what can
    hit the wrong item once the list has changed since the patch was made. Adding by place
    (a move's or copy's destination too) is left out: it loses nothing."""
    tested: list[list[str]] = []
    out = []
    for step in patch:
        op = step.get("op") if isinstance(step, dict) else None
        if op not in ("test", "remove", "replace", "move", "copy"):
            continue  # adding, a move's or copy's destination too, loses nothing
        try:
            tokens = pointer(step.get("from" if op in ("move", "copy") else "path"))
        except AppError:
            continue
        if op == "test":
            tested.append(tokens)
            continue
        places = [n for n, t in enumerate(tokens) if t.isdigit()]
        if not places:
            continue
        item = tokens[: places[-1] + 1]
        if not any(t[: len(item)] == item or item[: len(t)] == t for t in tested):
            out.append(spelled(tokens))
    return out


def apply(app: dict[str, Any], patch: Any) -> dict[str, Any]:
    """The app's owned parts ({title, card, data}) after `patch`, made in `app`'s own
    objects, which are spent either way (a 4 MB app isn't copied for each change): pass
    data read for it, and a patch parsed for it, whose values end up in the app."""
    if not isinstance(patch, list) or not patch:
        raise AppError(
            "patch must be a list of operations, e.g. "
            '[{"op": "add", "path": "/data/items/-", "value": {...}}]'
        )
    if len(patch) > MAX_OPS:
        raise AppError(f"a patch has at most {MAX_OPS} operations")
    doc = {k: app[k] for k in OWNED}
    for n, step in enumerate(patch, 1):
        if not isinstance(step, dict) or step.get("op") not in OPS:
            raise AppError(f"operation {n}: op must be one of: {', '.join(OPS)}")
        op, path = step["op"], step.get("path")
        if op in ("add", "replace", "test") and "value" not in step:
            raise AppError(f"operation {n} ({op}) needs a value")
        try:
            if op == "add":
                add(doc, path, step["value"])
            elif op == "remove":
                remove(doc, path)
            elif op == "replace":
                get(doc, path)  # it must be there already
                tokens = pointer(path)
                if len(tokens) == 1:
                    doc[tokens[0]] = step["value"]
                else:
                    remove(doc, path)
                    add(doc, path, step["value"])
            elif op == "test":
                if get(doc, path) != step["value"]:
                    raise AppError(f"{path} isn't what the patch expected")
            else:
                source = step.get("from")
                origin, target = pointer(source, "from"), pointer(path)
                if op == "move" and target[: len(origin)] == origin:
                    raise AppError(f"{source} can't move into itself")
                value = (
                    remove(doc, source)
                    if op == "move"
                    else copy.deepcopy(get(doc, source))
                )
                add(doc, path, value)
        except AppError as e:
            raise AppError(f"operation {n} ({op}): {e}") from None
    return doc
