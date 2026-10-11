"""The sandbox files `ingest` reads, and the chunks it cuts them into.

A path is a sandbox path as the agent knows it, in the caller's own folders: /work (the
chat's), /project (the workspace's) or /shared/<workspace>; a relative one is under /work.
They're the sandbox's folders on the host (hostenv.sandbox_mounts), which a run can
write, so every file is opened through hostrpc.safefs: no step of the way may be a
symlink, and only plain files are read. A folder is walked the same way, skipping hidden
entries. Nothing is made: a workspace that never ran code has no files.

Only text is taken: UTF-8 without NUL bytes. A PDF or an image isn't; the agent can turn
one into text with run-code first.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import hostenv
from hostrpc import RunnerError, safefs

MAX_FILE_BYTES = 1 << 20  # a file past this is cut there
MAX_FILES = 200  # files one ingest takes, a folder's included
CHUNK_CHARS = 1200  # about 300 tokens, well inside the model's 512
SKIP_DIRS = {"node_modules", "__pycache__", "site-packages"}


@dataclass(frozen=True)
class Folders:
    """A caller's sandbox folders on the host (hostenv.sandbox_mounts)."""

    workspace: str
    thread: str
    root: Path  # hostenv.sandbox_root(): every workspace's folder

    @property
    def mounts(self) -> dict[str, Path]:
        return hostenv.sandbox_mounts(
            self.root / self.workspace, self.workspace, self.thread
        )

    def split(self, path: str) -> tuple[str, Path, tuple[str, ...]]:
        """`path` as its mount, that mount's folder and the parts below it. '..' and
        paths outside the caller's own folders are refused."""
        path = path.strip()
        mount = next(
            (m for m in self.mounts if path == m or path.startswith(m + "/")), None
        )
        if mount is None and path.startswith("/"):
            raise RunnerError(
                f"'{path}' isn't in this chat's sandbox folders: give a path under "
                f"{', '.join(self.mounts)}"
            )
        rest = path.removeprefix(mount or "").strip("/")
        parts = tuple(p for p in rest.split("/") if p not in ("", "."))
        if ".." in parts:
            raise RunnerError(f"bad path '{path}': '..' isn't allowed")
        return mount or "/work", self.mounts[mount or "/work"], parts

    def name(self, mount: str, parts: tuple[str, ...]) -> str:
        return "/".join((mount, *parts))

    def found(self, path: str) -> list[str]:
        """The files `path` names, as sandbox paths: itself, or every file under a folder
        (hidden ones and caches skipped), at most MAX_FILES."""
        mount, root, parts = self.split(path)
        if not parts:
            return self._walk(root, mount, ())
        try:
            with safefs.folder(root, parts[:-1]) as parent:
                st = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        except OSError:
            raise RunnerError(f"there's no file or folder '{path}'") from None
        if stat.S_ISDIR(st.st_mode):
            return self._walk(root, mount, parts)
        if not stat.S_ISREG(st.st_mode):
            raise RunnerError(f"'{path}' isn't a plain file or folder")
        return [self.name(mount, parts)]

    def _walk(self, root: Path, mount: str, parts: tuple[str, ...]) -> list[str]:
        out: list[str] = []
        try:
            with safefs.folder(root, parts) as fd:
                self._walk_fd(fd, mount, parts, out)
        except OSError:
            raise RunnerError(
                f"there's no folder '{self.name(mount, parts)}'"
            ) from None
        return out

    def _walk_fd(
        self, fd: int, mount: str, parts: tuple[str, ...], out: list[str]
    ) -> None:
        with os.scandir(fd) as entries:
            listed = sorted(entries, key=lambda e: e.name)
        for entry in listed:
            if len(out) >= MAX_FILES:
                return
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name in SKIP_DIRS:
                    continue
                inner = os.open(entry.name, safefs.DIR_FLAGS, dir_fd=fd)
                try:
                    self._walk_fd(inner, mount, (*parts, entry.name), out)
                finally:
                    os.close(inner)
            elif entry.is_file(follow_symlinks=False):
                out.append(self.name(mount, (*parts, entry.name)))

    def text(self, path: str) -> str | None:
        """The text of the file at the sandbox path `path` (the first MAX_FILE_BYTES), or
        None if it isn't a plain UTF-8 text file."""
        _, root, parts = self.split(path)
        data = safefs.read_regular(root, parts, MAX_FILE_BYTES)
        if data is None or b"\0" in data:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as e:
            if e.start < len(data) - 3:  # not just a character cut at the limit
                return None
            return data[: e.start].decode("utf-8")


def chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """`text` in pieces of at most `size` characters, cut between paragraphs where it can,
    then between sentences, then anywhere."""
    out: list[str] = []
    current = ""
    for piece in _pieces(text, size):
        if current and len(current) + 2 + len(piece) > size:
            out.append(current)
            current = ""
        current = f"{current}\n\n{piece}" if current else piece
    if current:
        out.append(current)
    return out


def _pieces(text: str, size: int) -> Iterator[str]:
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= size:
            yield paragraph
            continue
        line = ""
        for sentence in re.split(r"(?<=[.!?])\s+|\n", paragraph):
            while len(sentence) > size:
                if line:
                    yield line
                    line = ""
                yield sentence[:size]
                sentence = sentence[size:]
            if line and len(line) + 1 + len(sentence) > size:
                yield line
                line = ""
            line = f"{line} {sentence}" if line else sentence
        if line:
            yield line
