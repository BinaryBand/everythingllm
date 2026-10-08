"""A workspace's browser container, as browser-runner starts and talks to it: the podman
run that hardens it (container_args), its Session (slot, address, sockets, who has it),
and the copying of its finished downloads to the workspace's /project/downloads
(collect_downloads), opening every step without following a symlink (hostrpc.safefs).
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from hostrpc import RunnerError, safefs

from browser.config import KEY_RE, Config
from browser.tabs import Approval

IMAGE = "localhost/everythingllm-browser"  # hostctl browser-images builds it
LABEL = "everythingllm-browser=1"
PREFIX = "everythingllm-browser-"  # + the workspace: its container's name
SCREEN = "1280x800"
MEMORY = "2g"
DOWNLOAD_BYTES = 256 << 20  # as browser.driver's: a bigger file isn't copied

PodmanResult = tuple[int, str, str]
Podman = Callable[[list[str], float], Awaitable[PodmanResult]]


async def podman(args: list[str], timeout: float) -> PodmanResult:
    """Run podman, giving up after `timeout`: (exit code, stdout, stderr), each cut short."""
    proc = await asyncio.create_subprocess_exec(
        "podman",
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"podman {args[0]} took over {timeout:.0f} s"
    return proc.returncode or 0, out.decode()[-4000:], err.decode()[-4000:]


@dataclass(eq=False)
class Session:
    """A workspace's running browser container."""

    workspace: str
    slot: str
    ip: str
    token: str  # the take-over view's path; new with each container
    folder: Path  # its sockets
    used: float
    control: str = "agent"  # or "user"
    reason: str = ""  # why the user has it
    asked: bool = False  # whether the agent asked for it (a handoff)
    viewers: int = 0  # take-over views open
    # Whether the driver may have logins the user sent to offer for saving: from when the
    # user has the browser until, the agent's again, it says it has none.
    offering: bool = False
    # id -> an OK the agent waits for, at most one per chat, in the order asked
    approvals: dict[str, Approval] = field(default_factory=dict)
    # (thread, login id) -> OK until: one chat's OK isn't another's
    granted: dict[tuple[str, str], float] = field(default_factory=dict)
    answers: dict[str, bool] = field(default_factory=dict)  # approval id -> the user's
    # Whether its pages can make a passkey, as the driver last said.
    making: bool = False
    made: str = ""  # what became of the last passkey made, for the take-over view

    @property
    def name(self) -> str:
        return PREFIX + self.workspace

    @property
    def driver(self) -> Path:
        return self.folder / "driver.sock"

    @property
    def vnc(self) -> Path:
        return self.folder / "vnc.sock"


class Gone(RunnerError):
    """The workspace's browser stopped under a call (its window was closed, or it crashed)."""


def container_args(config: Config, session: Session) -> list[str]:
    """The podman run for a workspace's browser: hardened like a service container, on
    egress-net at its slot's address with no DNS, its profile, downloads and sockets
    mounted (data only: noexec) and the repo read-only for the driver's code."""
    c, ws = config, session.workspace
    data = "rw,noexec,nosuid,nodev"
    src = c.repo / "packages"
    return [
        "run", "-d", "--rm", "--init",
        "--name", session.name, "--label", LABEL,
        "--read-only",
        "--tmpfs", "/tmp:rw,size=512m,mode=1777",
        "--tmpfs", "/var/lib/xkb:rw,size=8m,mode=1777",
        "--shm-size", "1g",
        "--memory", MEMORY, "--memory-swap", MEMORY, "--cpus", "1",
        "--pids-limit", "1024",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--userns", "keep-id",
        "--network", f"{c.network}:ip={session.ip}", "--dns", "none",
        "-e", f"BROWSER_PROXY={c.proxy}",
        "-e", f"BROWSER_SCREEN={SCREEN}",
        "-e", f"PYTHONPATH={src / 'browser' / 'src'}:{src / 'hostrpc' / 'src'}",
        "-v", f"{c.repo}:{c.repo}:ro",
        "-v", f"{c.profile(ws)}:/profile:{data}",
        "-v", f"{c.downloads(ws)}:/downloads:{data}",
        "-v", f"{session.folder}:/run/browser:{data}",
        IMAGE,
    ]  # fmt: skip


def collect_downloads(
    staging: Path, root: Path, workspace: str
) -> dict[str, list[str]]:
    """Move the finished downloads in `staging` (the container's /downloads, a folder per
    thread) to <root>/<workspace>/project/downloads/: thread -> what to tell it. Both ends
    are opened a step at a time without following a symlink, the one end because a sandbox
    run can write /project, the other because the browser can write /downloads; what isn't
    a plain file there is dropped, and a name that's taken gets -2, -3, … A dot file is one
    the driver is still saving."""
    told: dict[str, list[str]] = {}
    try:
        threads = sorted(os.listdir(staging))
    except FileNotFoundError:
        return told
    for thread in threads:
        if not KEY_RE.fullmatch(thread):
            continue
        try:
            folder = safefs.open_dir(staging, (thread,))
        except OSError:  # not a folder, or a symlink
            continue
        try:
            for name in sorted(os.listdir(folder)):
                if name.startswith("."):
                    continue
                try:
                    src = safefs.open_regular(folder, name)
                except OSError:
                    src = None
                with contextlib.suppress(OSError):
                    os.unlink(name, dir_fd=folder)
                if src is not None:
                    told.setdefault(thread, []).append(
                        copy_download(src, name, root, workspace)
                    )
        finally:
            os.close(folder)
    return told


def copy_download(src: int, name: str, root: Path, workspace: str) -> str:
    """Copy the download open at `src` (closed after) to /project/downloads: what to tell
    the thread."""
    with os.fdopen(src, "rb") as f:
        try:
            if os.fstat(f.fileno()).st_size > DOWNLOAD_BYTES:
                raise OSError(f"it's over {DOWNLOAD_BYTES >> 20} MB")
            with safefs.folder(
                root, (workspace, "project", "downloads"), make=True
            ) as d:
                fd, saved = safefs.create_free(d, name)
                with os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(f, out)
        except OSError as e:
            why = e.strerror or str(e)
            if e.errno in (errno.ELOOP, errno.ENOTDIR):
                why = "it isn't a plain folder"
            return f"couldn't save the download {name} to /project/downloads: {why}"
    return f"downloaded {saved} to /project/downloads/{saved}"
