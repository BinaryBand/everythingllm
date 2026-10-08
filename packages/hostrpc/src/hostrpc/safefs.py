"""Files in folders a container can write, opened by host code without following a symlink.

A service container (or a sandbox run) can put a symlink anywhere it can write. Host code
that opens a path there by name would follow it, and read or write wherever it points with
the host user's rights. These open each step relative to the folder before it, with
O_NOFOLLOW, so a symlink anywhere below the trusted `root` is refused (OSError, ELOOP or
ENOTDIR), and check the end is a plain file on the fd they hold, so the check can't be
raced. `root` itself is the caller's: it must be a folder no container can replace.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
from collections.abc import Iterator, Sequence
from pathlib import Path

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def open_dir(root: Path, parts: Sequence[str] = (), *, make: bool = False) -> int:
    """An fd for the folder `root`/`parts`, no step of it a symlink; with `make`, `root`
    and the folders in `parts` are made (0755) where missing."""
    if make:
        os.makedirs(root, 0o755, exist_ok=True)  # the caller's, trusted
    fd = os.open(root, DIR_FLAGS)
    try:
        for name in parts:
            if name in ("", ".", "..") or "/" in name:
                raise OSError(f"bad path part {name!r}")
            if make:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(name, 0o755, dir_fd=fd)
            inner = os.open(name, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = inner
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def folder(
    root: Path, parts: Sequence[str] = (), *, make: bool = False
) -> Iterator[int]:
    """open_dir, closed after."""
    fd = open_dir(root, parts, make=make)
    try:
        yield fd
    finally:
        os.close(fd)


def open_regular(
    dir_fd: int, name: str, flags: int = os.O_RDONLY, mode: int = 0o644
) -> int:
    """An fd for the plain file `name` in the open folder `dir_fd`: never a symlink, and
    never a FIFO, device or socket (it's refused, closed, with IsADirectoryError or OSError)."""
    fd = os.open(name, flags | FILE_FLAGS, mode, dir_fd=dir_fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(f"'{name}' isn't a regular file")
    return fd


def read_regular(root: Path, parts: Sequence[str], limit: int) -> bytes | None:
    """Up to `limit` bytes of the plain file `root`/`parts`; None if it isn't one (missing,
    a symlink on the way, or not a plain file)."""
    if not parts:
        return None
    try:
        with folder(root, parts[:-1]) as d:
            fd = open_regular(d, parts[-1])
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        return f.read(limit)


def create(dir_fd: int, name: str, mode: int = 0o644) -> int:
    """An fd for a new file `name` in the open folder `dir_fd`, for writing; FileExistsError
    if anything (a symlink included) is there already."""
    fd = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | FILE_FLAGS, mode, dir_fd=dir_fd
    )
    os.fchmod(fd, mode)  # whatever the umask
    return fd


def create_free(dir_fd: int, name: str, mode: int = 0o644) -> tuple[int, str]:
    """create() under `name`, or `stem-2.ext`, `stem-3.ext`, … if it's taken: (fd, name)."""
    stem, suffix = os.path.splitext(name)
    for n in range(1, 1000):
        candidate = name if n == 1 else f"{stem}-{n}{suffix}"
        try:
            return create(dir_fd, candidate, mode), candidate
        except FileExistsError:
            continue
    raise FileExistsError(name)


def replace(dir_fd: int, name: str, data: bytes, mode: int = 0o644) -> None:
    """Put `data` at `name` in the open folder `dir_fd` in one step, through a temp file of
    its own: a symlink at `name` is replaced, never followed."""
    tmp = f".{name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    fd = create(dir_fd, tmp, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise


def copy_tree(src: int, dest: Path, limit: int, *, hidden: bool = False) -> int:
    """Copy the plain files and folders under the open folder `src` into `dest` (a folder
    of the caller's, made if need be), leaving out symlinks, anything else that isn't a file
    or folder and, unless `hidden`, dot entries; OSError past `limit` bytes. The bytes
    copied."""
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    with os.scandir(src) as entries:
        listed = sorted((e.name, e.is_dir(follow_symlinks=False)) for e in entries)
    for name, is_dir in listed:
        if name.startswith(".") and not hidden:
            continue
        if is_dir:
            try:
                inner = os.open(name, DIR_FLAGS, dir_fd=src)
            except OSError:  # swapped for a symlink meanwhile
                continue
            try:
                total += copy_tree(inner, dest / name, limit - total, hidden=hidden)
            finally:
                os.close(inner)
            continue
        try:
            fd = open_regular(src, name)
        except OSError:  # a symlink, a FIFO, a device or socket
            continue
        with os.fdopen(fd, "rb") as f:
            total += os.fstat(f.fileno()).st_size
            if total > limit:
                raise OSError(f"over {limit} bytes to copy")
            with open(dest / name, "xb") as out:
                shutil.copyfileobj(f, out)
    return total
