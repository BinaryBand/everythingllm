"""A workspace's created files, for a client that holds the user's developer API key
(sandbox.appsweb serves them): what the agent's code left in the sandbox, listed, read,
kept and deleted, as the Nilson app's Created files view shows them.

A file's id is its path from the workspace's folder, the one thing a client sends back:

  project/<path>         kept: the agent's /project, browser downloads in project/downloads
  threads/<chat>/<path>  a working file, in one chat's /work; it goes THREAD_MAX_AGE after
                         the chat last used the sandbox (`expires`), unless it is kept
  public/<path>          on the web, at the workspace's pages site (`url`)
  shared/<path>          other workspaces can read it

An app's own files (project/apps, public/apps) are the apps' (sandbox.apps), and hidden
entries (.cache, .local...) are nobody's; neither is listed or reachable here. Every path
is opened a step at a time without following a symlink (hostrpc.safefs), since a run can
leave one anywhere it can write, and only plain files are listed, read, moved or deleted.
Deep research reports and AnythingLLM's own files are not in the sandbox, so not here.

Reading takes no lock; keeping and deleting take the workspace's (Runner.exclusive), so
neither happens under a run.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from hostrpc import safefs

from sandbox import workspace
from sandbox.errors import SandboxError
from sandbox.workspace import Scope

MAX_LISTED = 2000  # the newest; the answer says when it left some out
MAX_READ = 64 << 20  # a file bigger than this is not sent whole
APP_DIRS = {"project": "apps", "public": "apps"}  # the apps' own folders


class NoSuchFile(SandboxError):
    """A file id that names nothing, or nothing that may be named."""


class TooBig(SandboxError):
    """A file over MAX_READ."""


@dataclass(frozen=True)
class Place:
    """Where a file id points: the folder it is relative to and the steps inside it."""

    area: str  # project, work, public or shared
    root: Path
    parts: tuple[str, ...]
    thread: str | None = None


def place(s: Scope, file_id: str) -> Place:
    """The file id's place, refusing a hidden step, an app's folder and anything else that
    is not a path inside one of the workspace's own areas."""
    if "\0" in file_id:
        raise NoSuchFile("there's no such file")
    steps = tuple(file_id.strip().split("/"))
    kind, rest = steps[0], steps[1:]
    thread = None
    if kind == "threads" and len(rest) > 1:
        thread, rest = rest[0], rest[1:]
        area, root = "work", s.home / "threads" / thread
    elif kind in ("project", "shared") and rest:
        area, root = kind, s.home / kind
    elif kind == "public" and rest:
        area, root = "public", s.public
    else:
        raise NoSuchFile(f"there's no '{file_id}'")
    bad = (
        any(p in ("", ".", "..") or p.startswith(".") for p in steps[1:])
        or (thread is not None and workspace.KEY_RE.fullmatch(thread) is None)
        or rest[0] == APP_DIRS.get(area)
    )
    if bad:
        raise NoSuchFile(f"there's no '{file_id}'")
    return Place(area, root, rest, thread)


def listing(config: workspace.Config, s: Scope) -> dict:
    """Every created file, newest first, and the workspace against its limit."""
    found: list[dict] = []
    roots = [
        ("project", s.home / "project", "project", None),
        ("shared", s.home / "shared", "shared", None),
        ("public", s.public, "public", None),
    ]
    threads = s.home / "threads"
    if threads.is_dir() and not threads.is_symlink():
        for t in sorted(os.listdir(threads)):
            d = threads / t
            if workspace.KEY_RE.fullmatch(t) and d.is_dir() and not d.is_symlink():
                roots.append(("work", d, f"threads/{t}", t))
    for area, root, front, thread in roots:
        if root.is_symlink() or not root.is_dir():
            continue
        expires = (
            int(root.stat().st_mtime) + workspace.THREAD_MAX_AGE if thread else None
        )
        for rel, st in _files(root, APP_DIRS.get(area)):
            entry = {
                "id": f"{front}/{rel}",
                "name": rel.rsplit("/", 1)[-1],
                "dir": rel.rsplit("/", 1)[0] if "/" in rel else "",
                "size": st.st_size,
                "modified": int(st.st_mtime),
                "area": area,
            }
            if thread:
                entry |= {"thread": thread, "expires": expires}
            if area == "public":
                entry["url"] = config.public_url.rstrip("/") + f"/{s.workspace}/{rel}"
            found.append(entry)
    found.sort(key=lambda e: (-e["modified"], e["id"]))
    usage = workspace.snapshot(s, files=False)
    return {
        "workspace": s.workspace,
        "used": usage.total,
        "warn": workspace.WORKSPACE_WARN_BYTES,
        "limit": workspace.WORKSPACE_MAX_BYTES,
        "truncated": len(found) > MAX_LISTED,
        "files": found[:MAX_LISTED],
    }


def _files(root: Path, skip: str | None):
    """The plain, visible files below `root` as (path inside it, lstat), not entering a
    symlink or hidden folder, or `skip` at the top."""
    cut = len(os.fspath(root)) + 1
    for dirpath, dirnames, filenames in os.walk(root):
        inside = dirpath[cut:]
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".")
            and not (not inside and d == skip)
            and not os.path.islink(os.path.join(dirpath, d))
        ]
        for name in filenames:
            if name.startswith("."):
                continue
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except FileNotFoundError:
                continue
            if stat.S_ISREG(st.st_mode):
                yield (f"{inside}/{name}" if inside else name), st


def _require_regular(dir_fd: int, name: str, file_id: str) -> None:
    mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
    if not stat.S_ISREG(mode):
        raise NoSuchFile(f"there's no '{file_id}'")


def read(s: Scope, file_id: str) -> tuple[str, bytes]:
    """A file's name and bytes, refusing one over MAX_READ."""
    p = place(s, file_id)
    data = safefs.read_regular(p.root, p.parts, MAX_READ + 1)
    if data is None:
        raise NoSuchFile(f"there's no '{file_id}'")
    if len(data) > MAX_READ:
        raise TooBig(
            f"'{p.parts[-1]}' is over {MAX_READ >> 20} MB, too big to send here"
        )
    return p.parts[-1], data


def keep(s: Scope, file_id: str) -> dict:
    """Move a working file to the same path in /project, where it stays. Refuses a file
    that is not a working file, or a name /project already has."""
    p = place(s, file_id)
    if p.area != "work":
        raise SandboxError("only a working file needs keeping")
    project = s.home / "project"
    try:
        with (
            safefs.folder(p.root, p.parts[:-1]) as src,
            safefs.folder(project, p.parts[:-1], make=True) as dst,
        ):
            name = p.parts[-1]
            _require_regular(src, name, file_id)
            try:
                os.stat(name, dir_fd=dst, follow_symlinks=False)
            except FileNotFoundError:
                os.rename(name, name, src_dir_fd=src, dst_dir_fd=dst)
            else:
                raise SandboxError(f"/project already has '{'/'.join(p.parts)}'")
    except FileNotFoundError:
        raise NoSuchFile(f"there's no '{file_id}'") from None
    except NotADirectoryError:
        raise SandboxError("/project has a file where this one's folder goes") from None
    return {"id": "project/" + "/".join(p.parts), "kept": True}


def remove(s: Scope, file_id: str) -> dict:
    """Delete one plain file."""
    p = place(s, file_id)
    try:
        with safefs.folder(p.root, p.parts[:-1]) as d:
            _require_regular(d, p.parts[-1], file_id)
            os.unlink(p.parts[-1], dir_fd=d)
    except OSError:
        raise NoSuchFile(f"there's no '{file_id}'") from None
    return {"id": file_id, "deleted": True}
