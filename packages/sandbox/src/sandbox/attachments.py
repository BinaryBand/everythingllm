"""A chat's attachments, as text in its run's /work/attachments.

AnythingLLM keeps each attached file's text as <uploads>/<name>-<uuid>.json (the config's
uploads, SANDBOX_UPLOADS), the run-code skill names the chat's, and the runner copies them
in under the workspace's lock before the run (`sync_attachments`). A manifest beside them
says which source each copy came from, so the copy of a file no longer attached can go.
Nothing here follows a symlink: code in the sandbox can put one anywhere in /work."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

from hostrpc import safefs

from sandbox import workspace
from sandbox.workspace import Scope, snapshot

log = logging.getLogger("sandbox-runner")

ATTACHMENTS = "attachments"
# In it: each copy's source and hash, so a copy of a detached file can go.
MANIFEST = ".manifest.json"
ATTACHMENTS_MAX = 50  # named in one run
ATTACHMENT_BYTES = 50 << 20  # one source file, at most
ATTACHMENTS_BYTES = 200 << 20  # the sources read for one run, at most
UPLOAD_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,250}\.json$")
# Names that keep their extension; the rest become .txt.
TEXT_TYPES = ("csv", "tsv", "txt", "md", "json")
NAME_MAX = 100


def attachment_name(title: str, taken: set[str]) -> str:
    """A file name in /work/attachments for an attachment called `title`: NFC, letters,
    digits and ._- only, at most NAME_MAX characters, no leading dot, and not in `taken`.
    Text types keep their extension; anything else (a PDF's or a spreadsheet's text, with
    the sheets AnythingLLM names in its title) becomes <title>.txt."""
    title = unicodedata.normalize("NFC", title)[:500]
    clean = re.sub(r"[^\w.-]+", "_", title).strip("._-") or "attachment"
    stem, dot, ext = clean.rpartition(".")
    if not (dot and stem and ext.lower() in TEXT_TYPES):
        stem, ext = clean, "txt"
    stem = stem[: NAME_MAX - len(ext) - 4].rstrip("._-") or "attachment"
    for n in range(1, len(taken) + 2):
        name = f"{stem}.{ext}" if n == 1 else f"{stem}-{n}.{ext}"
        if name not in taken:
            return name
    raise AssertionError("unreachable: one of len(taken) + 1 names is free")


def safe_name(name: Any) -> bool:
    """A name a manifest (which code in the sandbox can edit) may give: one plain entry."""
    return (
        isinstance(name, str)
        and 0 < len(name) <= 255
        and "/" not in name
        and "\0" not in name
        and not name.startswith(".")
    )


def read_manifest(folder: int) -> dict[str, dict[str, Any]]:
    """/work/attachments/.manifest.json: {name: {source, sha256, bytes}} for each copy the
    runner wrote, leaving out whatever in it isn't one (code in the sandbox can write it)."""
    try:
        fd = safefs.open_regular(folder, MANIFEST)
    except OSError:
        return {}
    with os.fdopen(fd, "rb") as f:
        raw = f.read(1 << 20)
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        name: {"source": e["source"], "sha256": e["sha256"], "bytes": e["bytes"]}
        for name, e in data.items()
        if safe_name(name)
        and isinstance(e, dict)
        and isinstance(e.get("source"), str)
        and UPLOAD_RE.fullmatch(e["source"])
        and isinstance(e.get("sha256"), str)
        and isinstance(e.get("bytes"), int)
    }


def unchanged(folder: int, name: str, entry: dict[str, Any]) -> bool:
    """Whether the copy `name` is still what the runner wrote (False when it's gone or isn't
    a plain file)."""
    try:
        fd = safefs.open_regular(folder, name)
    except OSError:
        return False
    with os.fdopen(fd, "rb") as f:
        if os.fstat(f.fileno()).st_size != entry["bytes"]:
            return False
        digest = hashlib.sha256()
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest() == entry["sha256"]


def read_upload(uploads: Path, file: str, budget: int) -> tuple[bytes | None, int, str]:
    """An attachment's text from AnythingLLM's `uploads` folder, read without following a
    symlink and only if its file is at most `budget` bytes: (text, the file's size, "") or
    (None, bytes read, why not)."""
    try:
        with safefs.folder(uploads) as d:
            fd = safefs.open_regular(d, file)
    except FileNotFoundError:
        log.warning("attachment %s isn't in %s", file, uploads)
        return None, 0, "is no longer on the server"
    except OSError as e:
        return None, 0, f"couldn't be read ({e.strerror or e})"
    with os.fdopen(fd, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        if size > ATTACHMENT_BYTES:
            return None, 0, f"is over {ATTACHMENT_BYTES >> 20} MB"
        if size > budget:
            over = f"{ATTACHMENTS_BYTES >> 20} MB of attachments it copies"
            return None, 0, f"would take this run over the {over}"
        raw = f.read(size + 1)
    if len(raw) > size:  # it grew while it was read
        return None, len(raw), "changed while it was read"
    try:
        text = json.loads(raw).get("pageContent")
    except (ValueError, AttributeError):
        text = None
    if not isinstance(text, str):
        return None, len(raw), "has no text AnythingLLM kept"
    return text.encode(errors="replace"), len(raw), ""


def sync_attachments(
    uploads: Path, scope: Scope, attachments: Any, known: bool
) -> tuple[list[str], list[str]]:
    """Put the chat's attachments, [{title, file}] with `file` a JSON file in AnythingLLM's
    `uploads` folder, as text files in /work/attachments: (the names there now, notes on
    what couldn't be copied). Under the workspace's lock, before a run.

    Only new copies are written; a copy already there stays as it is, edited or not.
    /work/attachments/.manifest.json says which source each copy came from and what was
    written. When `known` (the skill's lookup was whole), a copy whose file is no
    longer attached goes, if it's unchanged; an edited one stays, as the chat's own. A
    source that's gone is skipped, and its copy kept. A gateway client's scope has no
    chat, and gets nothing. Nothing here follows a symlink: code in the sandbox can put
    one anywhere in /work."""
    if scope.gateway or not (attachments or known):
        return [], []
    notes: list[str] = []
    if not isinstance(attachments, list):
        attachments, known = [], False
    if len(attachments) > ATTACHMENTS_MAX:
        notes.append(f"only the first {ATTACHMENTS_MAX} attachments were copied")
        attachments, known = attachments[:ATTACHMENTS_MAX], False
    wanted: dict[str, str] = {}  # source file: title
    for a in attachments:
        file = a.get("file") if isinstance(a, dict) else None
        if not isinstance(file, str) or not UPLOAD_RE.fullmatch(file):
            notes.append("an attachment with a bad file name was skipped")
            continue
        title = a.get("title")
        wanted.setdefault(
            file,
            title if isinstance(title, str) and title.strip() else file[:-5],
        )
    work = scope.roots["/work"]
    try:
        folder = safefs.open_dir(work, (ATTACHMENTS,), make=bool(wanted))
    except FileNotFoundError:
        return [], notes  # nothing attached, and no copies to remove
    except OSError:
        notes.append(
            f"/work/{ATTACHMENTS} isn't a folder (a file or link is in its place), so "
            "the chat's attachments weren't copied"
        )
        return [], notes
    try:
        return fill_attachments(uploads, scope, folder, wanted, known, notes), notes
    finally:
        os.close(folder)
        with contextlib.suppress(OSError):
            os.rmdir(work / ATTACHMENTS)  # only if it's empty, and never a link


def fill_attachments(
    uploads: Path,
    scope: Scope,
    folder: int,
    wanted: dict[str, str],
    known: bool,
    notes: list[str],
) -> list[str]:
    """sync_attachments' work in the open folder /work/attachments."""
    manifest = read_manifest(folder)
    before = dict(manifest)
    there = set(os.listdir(folder))
    by_source = {e["source"]: name for name, e in manifest.items()}
    present: list[str] = []
    new: list[tuple[str, str, bytes]] = []  # (source, name, text)
    budget = ATTACHMENTS_BYTES
    for file, title in wanted.items():
        old = by_source.get(file)
        if old is not None and old in there:
            present.append(old)
            continue
        # A copy that's gone (the chat deleted it) is written again, under its name.
        text, read, why = read_upload(uploads, file, budget)
        budget -= read
        if text is None:
            notes.append(f"{title} {why}, so it wasn't copied")
            continue
        taken = there | set(manifest) | {n for _, n, _ in new}
        new.append((file, old or attachment_name(title, taken), text))
    if new:
        usage = snapshot(scope)
        room = workspace.WORKSPACE_MAX_BYTES - usage.total
        for file, name, text in new:
            if len(text) > room:
                notes.append(
                    f"{name} wasn't copied: the workspace's sandbox is near its "
                    f"{workspace.WORKSPACE_MAX_BYTES >> 20} MB limit"
                )
                continue
            try:
                safefs.replace(folder, name, text)
            except OSError as e:
                notes.append(f"{name} couldn't be written ({e.strerror or e})")
                continue
            room -= len(text)
            present.append(name)
            manifest[name] = {
                "source": file,
                "sha256": hashlib.sha256(text).hexdigest(),
                "bytes": len(text),
            }
    if known:
        for name, entry in list(manifest.items()):
            if entry["source"] in wanted:
                continue
            if unchanged(folder, name, entry):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=folder)
            del manifest[name]  # gone, or edited and now the chat's own
    if manifest != before:
        if manifest:
            data = json.dumps(manifest, indent=1, sort_keys=True).encode()
            safefs.replace(folder, MANIFEST, data)
        else:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(MANIFEST, dir_fd=folder)
    return sorted(present)
