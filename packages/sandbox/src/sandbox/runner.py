"""The sandbox runner: a host daemon that runs the agent's code in throwaway podman containers
and builds the sites it makes.

Each run gets a fresh container from the sandbox image with no network except the
allowlisting proxy on sandbox-net, a read-only root, CPU/memory/process limits and a
time limit. What it can see depends on where the call came from, which the skills in
AnythingLLM pass as a scope of {workspace, thread} (never chosen by the model):

  /work            the thread's scratch folder, deleted a week after the thread last used it
  /project         the workspace's folder, shared by its threads and kept (pip installs go here)
  /shared/<ws>     the workspace's shared folder: it writes it, every other workspace reads it
  /shared/<other>  each other workspace's shared folder, read-only
  /public          the workspace's pages on the web, served as they are at
                   https://<host>:8447/<workspace>/ the moment they're written
  /system/themes   the repo's Zola themes, read-only

Beside these, the workspace's folder holds its browser profile (browser/, packages/browser),
which no run mounts and the size limit leaves out; the browser saves downloads in
/project/downloads.

The shared and system folders are mounted noexec and never on PATH: they're data, and code
in another workspace's folder isn't to be run. A workspace's folders together (its shared
folder too) are held to WORKSPACE_MAX_BYTES. The script itself is mounted read-only from a
host-only folder at /sandbox.

Nothing is written by more than one workspace, so the workspace's lock is all the runner
needs: a run, a write and a publish in one workspace take turns, runs in different
workspaces overlap, and the runner's file operations only ever take paths in the caller's
own folders, which no other workspace can change under them.

Each workspace's /public lives apart from its other folders, in a tree that holds nothing
but /public folders (SANDBOX_PUBLIC, `<public>/<workspace>/`). The workspace pages site
(static_agent, host/caddy/pages.Caddyfile, :8447) serves that tree read-only as it is, each
workspace under its own prefix: there's no copy, no page to claim, and a half-written page
is the workspace's own business. Nothing private is in the tree, so a symlink in it can't
reach another workspace's /project or /work. The site's CSP is one rule for every
workspace (no scripts yet; the Caddyfile has the switch for letting a workspace's pages run
them, on an origin of their own). A run, write or build that changed /public says which
pages changed and where they are; op_publish gives a page's address and link card, and can
copy a file or folder into /public first.

A site build (op_build_site) runs the repo's sitebuild.py in a container with no network
and every folder read-only but an empty /out: it copies a Zola site from the workspace's
own folders, puts the theme it names in place (the repo's from /system/themes, or a
workspace's from /shared/<it>/themes), and builds it. The runner copies the output into
/public/<slug> (plain files only), so a site goes live like any page.

The system sites (news, research, status) are built the same way when their repo zola.toml
names a theme with [extra.build] theme_from (op_build_system_site, which sites.build calls):
their repo source and the repo's themes come in read-only, with a copy of their entries
made without following a symlink (the sites and research containers can write
pages/entries), and no workspace's /shared; the output goes, plain files only, into the
pages site's `.<site>.new`, which sites.build marks and swaps in. A page's title for its
card, and what its CSP blocks, are read the same way: a run could leave a symlink in
/public pointing at another workspace's files. The op takes only
a site's name, and reads what to build from the repo itself: its socket is reachable from
the AnythingLLM container. It is also served alone, with ping, on a second socket
(SANDBOX_BUILD_SOCKET, `SystemBuilds`), the one the sites and research service containers
mount: the full socket trusts the scope a caller names, so a container that parses the web
must not have it.

Config (environment):
  ANYTHINGLLM_STORAGE, PUBLIC_HOST
                    this machine's storage directory and tailnet name, from host.env
                    (default /srv/anythingllm/storage, and no name: links use 127.0.0.1)
  SANDBOX_SOCKET    the Unix socket to listen on (default <storage>/everythingllm/sandbox/runner.sock)
  SANDBOX_BUILD_SOCKET  the socket serving only build_system_site (default
                    <storage>/everythingllm/sandbox-build/runner.sock)
  SANDBOX_ROOT      workspace folders, host-only (default
                    ~/.local/share/everythingllm/sandbox/workspaces);
                    run scripts go in its `.runs` folder
  SANDBOX_SYSTEM_THEMES  the themes mounted at /system/themes (default the repo's
                         packages/sites/zola/themes)
  SANDBOX_SITES_SOURCE   the system sites' sources (default packages/sites/zola/sites)
  SANDBOX_SITES_CONTENT  their entries (default ~/.local/share/everythingllm/pages/entries,
                         as sites.build's SITES_CONTENT)
  SANDBOX_PUBLIC    every workspace's /public, as <workspace>/ (default
                    ~/.local/share/everythingllm/sandbox/public)
  SANDBOX_PUBLIC_URL  public URL of SANDBOX_PUBLIC (default https://<PUBLIC_HOST>:8447/)
  SANDBOX_SITE_DIR  the pages site's root, where system sites are staged and link cards
                    saved (default ~/.local/share/everythingllm/pages/public)
  SANDBOX_SITE_URL  public URL of SANDBOX_SITE_DIR (default https://<PUBLIC_HOST>:8445/)
"""

from __future__ import annotations

import asyncio
import html as htmllib
import logging
import os
import re
import secrets
import shutil
import signal
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import chatimage.card
import hostrpc
import tomllib
from hostrpc import safefs

log = logging.getLogger("sandbox-runner")
# Big enough for a run's output, which the runner caps well below this.
LIMIT = 8 * 1024 * 1024


class SandboxError(hostrpc.RunnerError):
    """An error to show the agent: bad arguments, a missing file, the runner being down."""


# Also named in hostctl's sandbox-images (cli.py) and host/systemd/sandbox-proxy.service; the proxy's
# address (10.89.77.2:8888) is set in Containerfile.sandbox, that unit and tinyproxy.conf.
IMAGE = "localhost/everythingllm-sandbox"
NETWORK = "sandbox-net"
PROXY_CONTAINER = "sandbox-proxy"
LABEL = "everythingllm-sandbox=1"

LANGUAGES = {"python": ("main.py", "python"), "bash": ("main.sh", "bash")}
KEY_RE = re.compile(r"^[a-z0-9_][a-z0-9_-]{0,99}$")  # workspace slugs and thread ids
# The MCP gateway's clients' workspaces (gateway.sandbox): kept for scopes that say
# "gateway": true, which AnythingLLM's skills never do, so a workspace someone happens to
# name "Client X" can't share a gateway client's folders.
CLIENT_PREFIX = "client-"
SLUG_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)  # as sites.store.NAME_RE
# Shared and system folders are data: nothing in them runs as ./file.
DATA_RW = "rw,noexec,nosuid,nodev"
DATA_RO = "ro,noexec,nosuid,nodev"
REPO = Path(__file__).resolve().parents[4]  # <repo>/packages/sandbox/src/sandbox/
SYSTEM_ZOLA = REPO / "packages" / "sites" / "zola"  # the system sites and their themes
MEMORY = "1g"
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 300
BUILD_TIMEOUT = 60  # a site build, assembling included
SYSTEM_BUILD_TIMEOUT = (
    40  # a system site's, as sites.build's; its callers give up after 55
)
SITEBUILD = Path(__file__).with_name("sitebuild.py")  # the build helper, from the repo
MAX_PARALLEL = 2
OUTPUT_BYTES = 20_000  # per stream of a run
WRITE_BYTES = 1_000_000  # op_write
WORKSPACE_WARN_BYTES = 4 << 30
WORKSPACE_MAX_BYTES = 5 << 30  # no new runs or writes past this; deletes still work
PUBLISH_MAX_BYTES = 500 << 20  # what publish or a build copies into /public at once
THREAD_MAX_AGE = 7 * 24 * 3600
# The workspace's browser profile, in its folder beside the sandbox's (packages/browser).
BROWSER = "browser"
LIST_MAX = 200  # files named in a run's changed list
# A request answers within WAIT; a run that's still going carries on, and the skill waits
# on it again with op_wait.
WAIT = 45
RESULT_KEEP = 3600  # how long a finished run's result can still be fetched
CSP_SCAN = 200  # HTML files of a changed page checked for what the CSP blocks
ENTRIES_BYTES = 64 << 20  # a system site's entries copied into its build, at most

# What podman produced: exit code, stdout, stderr, and whether it was killed for time.
PodmanResult = tuple[int, str, str, bool]
Podman = Callable[[list[str], float | None, str | None], Awaitable[PodmanResult]]


def joined(head: bytes | bytearray, tail: bytes | bytearray, cut: int) -> str:
    """Text kept from both ends of something longer; the end of a traceback matters most."""
    gap = f"\n… [{cut} bytes cut] …\n" if cut else ""
    return head.decode(errors="replace") + gap + tail.decode(errors="replace")


async def capture(stream: asyncio.StreamReader, limit: int) -> str:
    """Read a stream to the end, keeping its first tenth of `limit` bytes and the rest from its end."""
    keep_head = limit // 10
    keep_tail = limit - keep_head
    head = bytearray()
    tail = bytearray()
    cut = 0
    while chunk := await stream.read(65536):
        if len(head) < keep_head:
            take = keep_head - len(head)
            head += chunk[:take]
            chunk = chunk[take:]
        tail += chunk
        if len(tail) > keep_tail:
            cut += len(tail) - keep_tail
            del tail[: len(tail) - keep_tail]
    return joined(head, tail, cut)


async def podman(
    args: list[str], timeout: float | None, kill: str | None
) -> PodmanResult:
    """Run podman; on timeout, kill the container named `kill` and report it."""
    proc = await asyncio.create_subprocess_exec(
        "podman",
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None and proc.stderr is not None  # both are PIPEs
    reading = asyncio.gather(
        capture(proc.stdout, OUTPUT_BYTES), capture(proc.stderr, OUTPUT_BYTES)
    )
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except TimeoutError:
        timed_out = True
        if kill:
            killer = await asyncio.create_subprocess_exec(
                "podman",
                "kill",
                kill,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        try:
            await asyncio.wait_for(proc.wait(), 30)
        except TimeoutError:
            proc.kill()
            await proc.wait()
    out, err = await reading
    return proc.returncode or 0, out, err, timed_out


@dataclass
class Config:
    socket: Path
    root: Path
    system_themes: Path
    site_dir: Path
    site_url: str
    public_root: Path = Path("/nonexistent")
    public_url: str = "http://127.0.0.1:8447/"
    sites_source: Path = SYSTEM_ZOLA / "sites"
    sites_content: Path = Path("/nonexistent")
    build_socket: Path | None = None  # SystemBuilds' socket; none, none served

    @property
    def scripts(self) -> Path:
        return self.root / ".runs"

    @classmethod
    def from_env(cls) -> Config:
        get = os.environ.get
        host = get("PUBLIC_HOST")
        return cls(
            socket=hostrpc.socket_path("sandbox", "SANDBOX_SOCKET"),
            build_socket=hostrpc.socket_path("sandbox-build", "SANDBOX_BUILD_SOCKET"),
            root=Path(
                get("SANDBOX_ROOT", hostrpc.data_dir() / "sandbox" / "workspaces")
            ),
            system_themes=Path(get("SANDBOX_SYSTEM_THEMES", SYSTEM_ZOLA / "themes")),
            sites_source=Path(get("SANDBOX_SITES_SOURCE", SYSTEM_ZOLA / "sites")),
            sites_content=Path(
                get("SANDBOX_SITES_CONTENT", hostrpc.data_dir() / "pages" / "entries")
            ),
            site_dir=Path(get("SANDBOX_SITE_DIR", hostrpc.site_dir())),
            site_url=get(
                "SANDBOX_SITE_URL",
                f"https://{host}:8445/" if host else "http://127.0.0.1:8445/",
            ),
            public_root=Path(
                get("SANDBOX_PUBLIC", hostrpc.data_dir() / "sandbox" / "public")
            ),
            public_url=get(
                "SANDBOX_PUBLIC_URL",
                f"https://{host}:8447/" if host else "http://127.0.0.1:8447/",
            ),
        )


@dataclass(frozen=True)
class Scope:
    """Where a call came from, and the host folders behind its writable mounts: in the
    workspace's folder (`home`), threads/<thread>/ (/work), project/ (/project) and shared/
    (/shared/<workspace>); and its folder in the served tree (`public`, /public)."""

    workspace: str
    thread: str
    home: Path
    public: Path

    @property
    def roots(self) -> dict[str, Path]:
        return {
            "/work": self.home / "threads" / self.thread,
            "/project": self.home / "project",
            f"/shared/{self.workspace}": self.home / "shared",
            "/public": self.public,
        }

    def mount_of(self, folder: Path) -> tuple[str, tuple[str, ...]] | None:
        """A folder as its mount and its parts inside that; None outside them all
        (another thread's /work)."""
        for mount, root in self.roots.items():
            if folder.is_relative_to(root):
                return mount, folder.relative_to(root).parts
        return None


@dataclass
class Usage:
    """What a workspace holds, from one walk of its folder."""

    files: dict[str, tuple[int, int]] = field(
        default_factory=dict
    )  # sandbox path: (mtime, size)
    total: int = 0
    tops: dict[str, int] = field(
        default_factory=dict
    )  # size by top-level entry of each mount

    def biggest(self, n: int = 5) -> str:
        """For the error that stops a workspace over WORKSPACE_MAX_BYTES: no run can look then."""
        ranked = sorted(self.tops.items(), key=lambda kv: -kv[1])[:n]
        return ", ".join(f"{k} {v >> 20} MB" for k, v in ranked)


def walk(scope: Scope):
    """os.walk of the workspace's folder and its /public, leaving out its browser profile
    (browser-runner's, beside the sandbox's folders): no run sees it, and it isn't the
    sandbox's to hold to the workspace's limit."""
    for dirpath, dirnames, filenames in os.walk(scope.home):
        if dirpath == str(scope.home) and BROWSER in dirnames:
            dirnames.remove(BROWSER)
        yield dirpath, dirnames, filenames
    yield from os.walk(scope.public)


def snapshot(scope: Scope) -> Usage:
    """Every visible file in the workspace's mounts by its path in the sandbox, and the size
    of the whole workspace (its other threads and its /public too). Hidden top-level entries
    (.local with pip installs, .cache…) count toward the size only."""
    usage = Usage()
    for dirpath, _, filenames in walk(scope):
        where = scope.mount_of(Path(dirpath))
        for name in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except FileNotFoundError:
                continue
            usage.total += st.st_size
            if where is None:
                key = "other chats' /work"
            else:
                mount, parts = where[0], (*where[1], name)
                key = f"{mount}/{parts[0]}" + ("/" if len(parts) > 1 else "")
                if not parts[0].startswith("."):
                    usage.files[f"{mount}/{'/'.join(parts)}"] = (
                        st.st_mtime_ns,
                        st.st_size,
                    )
            usage.tops[key] = usage.tops.get(key, 0) + st.st_size
    return usage


def public_changes(before: Usage, after: Usage) -> set[str]:
    """The top-level entries of /public a run added, changed or removed: its pages that changed."""
    b, a = (
        {p: sig for p, sig in u.files.items() if p.startswith("/public/")}
        for u in (before, after)
    )
    return {p.split("/")[2] for p in b.keys() | a.keys() if b.get(p) != a.get(p)}


def write_regular(target: Path, data: bytes, path: str) -> None:
    """Write a file, refusing anything already there that isn't a plain file: opening a FIFO
    would block, and a device or socket isn't something to write to."""
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(st.st_mode):
            raise SandboxError(f"'{path}' exists and isn't a regular file")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o644,
    )
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def force_remove(func: Callable[..., Any], path: str, exc: BaseException) -> None:
    """rmtree's onexc: sandbox code can leave read-only folders behind, so make the folder
    and its parent writable and try again."""
    if isinstance(exc, FileNotFoundError):
        return
    try:
        for p in (os.path.dirname(path), path):
            if os.path.isdir(p) and not os.path.islink(p):
                os.chmod(p, os.stat(p).st_mode | stat.S_IRWXU)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, onexc=force_remove)
        else:
            os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("couldn't remove %s: %s", path, e)


def copy_regular(source: Path, dest: Path) -> None:
    """Copy a plain file without following a symlink or opening a FIFO: the checks before
    the copy can't be raced, because they're made on the file that's open."""
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as src:
        if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
            raise SandboxError(f"'{source.name}' isn't a regular file")
        with open(dest, "wb") as out:
            shutil.copyfileobj(src, out)


DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def open_dir(path: Path) -> int:
    """An fd for the folder `path`, refusing a symlink as its last part."""
    return os.open(path, DIR_FLAGS)


def make_dir(parent: int, name: str) -> int:
    """Make the folder `name` in the open folder `parent` if it isn't there, and open it,
    refusing a symlink in its place."""
    try:
        os.mkdir(name, 0o755, dir_fd=parent)
    except FileExistsError:
        pass
    return os.open(name, DIR_FLAGS, dir_fd=parent)


def copy_into(source: Path, folder: int, name: str) -> None:
    """Copy the plain file `source` to a new file `name` in the open folder `folder`
    (mode 644): never through a symlink, and never over a file that's already there."""
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as src:
        if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
            raise SandboxError(f"'{source.name}' isn't a regular file")
        out_fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
            dir_fd=folder,
        )
        with os.fdopen(out_fd, "wb") as out:
            os.fchmod(out.fileno(), 0o644)
            shutil.copyfileobj(src, out)


def remove_path(path: Path) -> None:
    """Delete a file, symlink or folder; a symlink itself, never what it points to."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, onexc=force_remove)
    else:
        path.unlink(missing_ok=True)


# What the pages site's CSP blocks, so a page that leans on it renders without it.
# Links to other sites are fine; loading from them isn't. Tags are matched with [^<>]*, so
# a page full of stray "<" can't make a pattern scan to the end of it again and again.
_OFFSITE = r"""["']?\s*(?:https?:)?//"""
_CSP_BLOCKED = (
    ("scripts", re.compile(r"<script\b", re.IGNORECASE)),
    (
        "inline event handlers (onclick= and the like)",
        re.compile(r"<[^<>]*\son[a-z]+\s*=", re.IGNORECASE),
    ),
    (
        "javascript: links",
        re.compile(r"""(?:href|src)\s*=\s*["']?\s*javascript:""", re.IGNORECASE),
    ),
    (
        "stylesheets or fonts from another host",
        re.compile(
            r"<link\b[^<>]*\bhref\s*=" + _OFFSITE + r"|@import\s*" + _OFFSITE,
            re.IGNORECASE,
        ),
    ),
    (
        "images, media or frames from another host",
        re.compile(
            r"<(?:img|iframe|video|audio|source|embed|object|track)\b[^<>]*\b(?:src|data|srcset)\s*="
            + _OFFSITE,
            re.IGNORECASE,
        ),
    ),
    (
        "CSS url()s pointing at another host",
        re.compile(r"url\(" + _OFFSITE, re.IGNORECASE),
    ),
)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
DESCRIPTION_RE = re.compile(
    r"""<meta\b[^>]*\bname\s*=\s*["']?description\b[^>]*\bcontent\s*=\s*(["'])(.*?)\1""",
    re.IGNORECASE | re.DOTALL,
)
PARAGRAPH_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]*>")


def csp_blocked(html: str, origin: str = "") -> list[str]:
    """What in a page the site's CSP will block: [] when nothing is. URLs on the site's own
    `origin` (scheme://host:port, as zola writes them) are allowed."""
    if origin:  # only where the origin ends: https://site.example.evil/ is another host
        html = re.sub(re.escape(origin) + r"""(?=[/"'\s>)]|$)""", "", html)
    return [what for what, pattern in _CSP_BLOCKED if pattern.search(html)]


def page_description(html: str) -> str:
    """A line about a page for its card: its meta description, else its first paragraph."""
    if m := DESCRIPTION_RE.search(html):
        return htmllib.unescape(m.group(2))
    for m in PARAGRAPH_RE.finditer(html):
        if text := " ".join(htmllib.unescape(TAG_RE.sub(" ", m.group(1))).split()):
            return text
    return ""


def page_path(slug: str, entry: str) -> str:
    """A page's path on the site: its folder when the entry is index.html, else the file."""
    return f"{slug}/" + ("" if entry == "index.html" else quote(entry))


def regular_files(source: Path) -> tuple[list[tuple[Path, str]], int]:
    """Every plain file under `source` with its path inside it, and their size; symlinks,
    anything else that isn't a file or folder, and hidden entries (.git, .cache…) are left
    out, as is `source` itself if it's a symlink."""
    st = os.lstat(source)
    if stat.S_ISREG(st.st_mode):
        name = (
            "index.html" if source.suffix.lower() in (".html", ".htm") else source.name
        )
        return [(source, name)], st.st_size
    if not stat.S_ISDIR(st.st_mode):
        return [], 0
    found, size = [], 0
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".") and not os.path.islink(os.path.join(dirpath, d))
        ]
        for name in filenames:
            if name.startswith("."):
                continue
            p = Path(dirpath, name)
            st = os.lstat(p)
            if stat.S_ISREG(st.st_mode):
                found.append((p, str(p.relative_to(source))))
                size += st.st_size
    return sorted(found, key=lambda f: f[1]), size


@dataclass
class Job:
    """A run, kept for RESULT_KEEP after it finishes so op_wait can still fetch its result."""

    workspace: str
    task: asyncio.Task[dict[str, Any]]
    started: float
    finished: float | None = None


@dataclass
class Runner(hostrpc.Service):
    log = log

    config: Config
    podman: Podman = podman
    now: Callable[[], float] = time.time
    _runs: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(MAX_PARALLEL)
    )
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _jobs: dict[str, Job] = field(default_factory=dict)

    # --- scopes and paths ---

    def scope(self, scope: dict[str, Any]) -> Scope:
        """The caller's folders, made on first use; using the thread's /work keeps it from gc."""
        if not isinstance(scope, dict):
            raise SandboxError("scope must be {workspace, thread}")
        workspace, thread = (
            str(scope.get("workspace") or ""),
            str(scope.get("thread") or ""),
        )
        for what, key in (("workspace", workspace), ("thread", thread)):
            if not KEY_RE.match(key):
                raise SandboxError(f"bad {what} '{key}'")
        gateway = scope.get("gateway") is True
        if workspace.startswith(CLIENT_PREFIX) != gateway:
            raise SandboxError(
                f"a gateway client's scope is a '{CLIENT_PREFIX}' workspace"
                if gateway
                else f"workspace '{workspace}': names starting '{CLIENT_PREFIX}' are the "
                "MCP gateway's clients' sandboxes; rename the workspace to use the sandbox"
            )
        s = Scope(
            workspace,
            thread,
            self.config.root / workspace,
            self.config.public_root / workspace,
        )
        for d in s.roots.values():
            d.mkdir(parents=True, exist_ok=True)
        os.utime(s.roots["/work"])
        return s

    def split(self, scope: Scope, path: str) -> tuple[str, Path, Path]:
        """`path` in the sandbox (under one of the caller's own mounts, or relative to /work)
        as its mount, that mount's host folder and the path inside it, unresolved. `..` is
        refused, and so is anything outside the caller's own folders: another workspace's
        /shared is read-only, and the runner never touches a folder another workspace writes."""
        path = path.strip()
        mount = next(
            (m for m in scope.roots if path == m or path.startswith(m + "/")), None
        )
        if mount is None and path.startswith("/"):
            owner = path.split("/")[2] if path.startswith("/shared/") else ""
            if owner and owner != scope.workspace:
                raise SandboxError(
                    f"'{path}' is in {owner}'s shared folder, which is read-only here; "
                    "copy what you need into your own folders with run-code"
                )
            raise SandboxError(
                f"bad path '{path}': use a path under {', '.join(scope.roots)}"
            )
        rel = Path(path.removeprefix(mount or "").lstrip("/"))
        if ".." in rel.parts:
            raise SandboxError(f"bad path '{path}': '..' isn't allowed")
        root = scope.roots[mount or "/work"]
        return mount or "/work", root, root / rel

    def resolve(self, scope: Scope, path: str) -> Path:
        """`path` after following symlinks, which must stay inside its mount's folder (not be
        all of it). Callers hold the workspace's lock, so no run can swap a link in after."""
        mount, root, target = self.split(scope, path)
        target, root = target.resolve(), root.resolve()
        if not target.is_relative_to(root):
            raise SandboxError(f"'{path}' points outside {mount}")
        if target == root:
            raise SandboxError(
                f"'{path}' is all of {mount}; give a file or folder inside it"
            )
        return target

    def lock(self, workspace: str) -> asyncio.Lock:
        return self._locks.setdefault(workspace, asyncio.Lock())

    def idle(self, workspace: str) -> None:
        """Fail fast while a run holds the workspace (another chat's, or one whose chat
        closed), rather than queueing for its lock past the caller's patience."""
        for job in self._jobs.values():
            if job.workspace == workspace and not job.task.done():
                left = max(0, MAX_TIMEOUT - (self.now() - job.started))
                raise SandboxError(
                    f"code is still running in this workspace (at most {left:.0f} s more); try again after"
                )

    def gc(self) -> list[str]:
        """Delete threads' /work folders untouched for a week. /project stays."""
        cutoff = self.now() - THREAD_MAX_AGE
        old = [
            d
            for d in self.config.root.glob("*/threads/*")
            if d.is_dir() and d.stat().st_mtime < cutoff
        ]
        for d in old:
            remove_path(d)
        return [f"{d.parent.parent.name}/{d.name}" for d in old]

    # --- running code ---

    async def op_ping(self) -> dict[str, Any]:
        image, network, proxy = await asyncio.gather(
            self.podman(["image", "exists", IMAGE], 30, None),
            self.podman(["network", "exists", NETWORK], 30, None),
            self.podman(
                [
                    "container",
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    PROXY_CONTAINER,
                ],
                30,
                None,
            ),
        )
        problems = []
        if image[0] != 0:
            problems.append(f"image {IMAGE} is missing (uv run hostctl sandbox-setup)")
        if network[0] != 0:
            problems.append(
                f"network {NETWORK} is missing (uv run hostctl sandbox-setup)"
            )
        if proxy[0] != 0 or proxy[1].strip() != "true":
            problems.append(
                f"{PROXY_CONTAINER} isn't running (systemctl --user status sandbox-proxy)"
            )
        return {"problems": problems}

    def prune(self) -> None:
        cutoff = self.now() - RESULT_KEEP
        for run_id in [
            i
            for i, j in self._jobs.items()
            if j.finished is not None and j.finished < cutoff
        ]:
            del self._jobs[run_id]

    async def wait(self, run_id: str, job: Job) -> dict[str, Any]:
        """The run's result if it finishes within WAIT, else a note that it's still going.
        Shielded, so giving up on the wait leaves the run alone."""
        try:
            result = await asyncio.wait_for(asyncio.shield(job.task), WAIT)
        except TimeoutError:
            return {
                "run_id": run_id,
                "running": True,
                "seconds": round(self.now() - job.started, 1),
            }
        return {**result, "run_id": run_id}

    async def op_run(
        self,
        scope: dict[str, Any],
        language: str,
        code: str,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        if language not in LANGUAGES:
            raise SandboxError(f"language must be one of: {', '.join(LANGUAGES)}")
        if not isinstance(code, str) or not code.strip():
            raise SandboxError("code is empty")
        timeout = max(1, min(int(timeout), MAX_TIMEOUT))
        s = self.scope(scope)
        self.prune()
        run_id = f"r-{secrets.token_hex(4)}"
        job = Job(
            s.workspace,
            asyncio.create_task(self.execute(s, language, code, timeout)),
            self.now(),
        )
        job.task.add_done_callback(lambda _: setattr(job, "finished", self.now()))
        self._jobs[run_id] = job
        return await self.wait(run_id, job)

    async def op_wait(self, scope: dict[str, Any], run_id: str) -> dict[str, Any]:
        workspace = self.scope(scope).workspace
        self.prune()
        if not (job := self._jobs.get(run_id)) or job.workspace != workspace:
            raise SandboxError(
                "no such run (it finished over an hour ago, or the runner restarted)"
            )
        return await self.wait(run_id, job)

    def over_quota(self, usage: Usage, doing: str) -> SandboxError:
        return SandboxError(
            f"this workspace's sandbox uses {usage.total >> 20} MB, over its {WORKSPACE_MAX_BYTES >> 20} MB "
            f"limit, so it can't {doing}. Delete something with write-file (delete=true) first; "
            f"the biggest: {usage.biggest()}."
        )

    async def execute(
        self, scope: Scope, language: str, code: str, timeout: int
    ) -> dict[str, Any]:
        """One run, start to finish; op_run keeps it as a task, which holds the workspace's
        lock throughout and one of the MAX_PARALLEL slots while the container runs."""
        script, interpreter = LANGUAGES[language]
        name = f"sandbox-{secrets.token_hex(6)}"
        run_dir = self.config.scripts / name
        async with self.lock(scope.workspace):
            before = await asyncio.to_thread(snapshot, scope)
            if before.total > WORKSPACE_MAX_BYTES:
                raise self.over_quota(before, "run code")
            try:
                args = await asyncio.to_thread(
                    self.prepare, name, scope, run_dir, script, code
                )
                async with self._runs:
                    started = self.now()
                    exit_code, out, err, timed_out = await self.podman(
                        args + [interpreter, f"/sandbox/{script}"], timeout, name
                    )
                    took = self.now() - started
                    oom = False
                    if exit_code == 137 and not timed_out:
                        _, state, _, _ = await self.podman(
                            ["inspect", "--format", "{{.State.OOMKilled}}", name],
                            30,
                            None,
                        )
                        oom = state.strip() == "true"
                after = await asyncio.to_thread(snapshot, scope)
            finally:
                await self.podman(["rm", "-f", "--ignore", name], 60, None)
                await asyncio.to_thread(shutil.rmtree, run_dir, True)
            published = await asyncio.to_thread(
                self.page_changes, scope, public_changes(before, after)
            )
        changed = sorted(
            p for p, sig in after.files.items() if before.files.get(p) != sig
        )
        log.info(
            "run workspace=%s thread=%s lang=%s exit=%s timed_out=%s oom=%s %.1fs",
            scope.workspace,
            scope.thread,
            language,
            exit_code,
            timed_out,
            oom,
            took,
        )
        return {
            "exit_code": exit_code,
            "timed_out": timed_out,
            "oom_killed": oom,
            "timeout": timeout,
            "seconds": round(took, 1),
            "stdout": out,
            "stderr": err,
            "changed": changed[:LIST_MAX],
            "changed_more": max(0, len(changed) - LIST_MAX),
            "published": published,
            "warning": (
                f"this workspace's sandbox uses {after.total >> 20} MB of its {WORKSPACE_MAX_BYTES >> 20} MB; "
                "delete what isn't needed"
            )
            if after.total > WORKSPACE_WARN_BYTES
            else None,
        }

    def hardening(self, name: str) -> list[str]:
        """Every sandbox container's podman arguments up to its network and mounts."""
        return [
            "run",
            "--name",
            name,
            "--label",
            LABEL,
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=256m,mode=1777",
            "--memory",
            MEMORY,
            "--memory-swap",
            MEMORY,
            "--cpus",
            "1",
            "--pids-limit",
            "256",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--userns",
            "keep-id",
        ]

    def read_only_mounts(self, workspace: str) -> list[str]:
        """Every other workspace's shared folder and the repo's themes, read-only."""
        return [
            *(
                a
                for other, folder in self.others_shared(workspace)
                for a in ("-v", f"{folder}:/shared/{other}:{DATA_RO}")
            ),
            "-v",
            f"{self.config.system_themes}:/system/themes:{DATA_RO}",
        ]

    def prepare(
        self, name: str, scope: Scope, run_dir: Path, script: str, code: str
    ) -> list[str]:
        """Write the run's script and return its podman arguments: the workspace's own
        folders read-write, every other workspace's shared folder and the repo's themes
        read-only."""
        (run_dir / "code").mkdir(parents=True)
        (run_dir / "code" / script).write_text(code)
        # No --rm: the container is kept until execute has asked podman whether it ran out of memory.
        return [
            *self.hardening(name),
            "--network",
            NETWORK,
            "--dns",
            "none",
            *(
                a
                for mount, root in scope.roots.items()
                for a in (
                    "-v",
                    f"{root}:{mount}"
                    + (
                        f":{DATA_RW}"
                        if mount == "/public" or mount.startswith("/shared/")
                        else ""
                    ),
                )
            ),
            *self.read_only_mounts(scope.workspace),
            "-v",
            f"{run_dir / 'code'}:/sandbox:ro",
            IMAGE,
        ]

    # --- site builds ---

    async def op_build_site(
        self, scope: dict[str, Any], path: str, slug: str = ""
    ) -> dict[str, Any]:
        """Build the Zola site in `path` (a folder in the workspace's own /project,
        /shared/<workspace> or /work) into /public/<slug>, and publish it. The slug is the
        folder's name unless given. A build that outlasts the call goes on, like a run."""
        s = self.scope(scope)
        mount, _, _ = self.split(s, path)
        if mount == "/public":
            raise SandboxError(
                "build a site from its source in /project or /shared, not from /public"
            )
        slug = slug or Path(path.strip().rstrip("/")).name
        if not SLUG_RE.fullmatch(slug):
            raise SandboxError(
                f"'{slug}' isn't a page name; give slug: 1-63 lowercase letters, digits "
                "or hyphens"
            )
        self.idle(s.workspace)
        self.prune()
        run_id = f"r-{secrets.token_hex(4)}"
        job = Job(
            s.workspace,
            asyncio.create_task(self.build(s, path.strip().rstrip("/"), slug)),
            self.now(),
        )
        job.task.add_done_callback(lambda _: setattr(job, "finished", self.now()))
        self._jobs[run_id] = job
        return await self.wait(run_id, job)

    async def build(self, scope: Scope, path: str, slug: str) -> dict[str, Any]:
        """One site build, under the workspace's lock: the helper in a container with no
        network, then the output copied into /public/<slug> and synced."""
        name = f"sandbox-{secrets.token_hex(6)}"
        run_dir = self.config.scripts / name
        async with self.lock(scope.workspace):
            usage = await asyncio.to_thread(snapshot, scope)
            if usage.total > WORKSPACE_MAX_BYTES:
                raise self.over_quota(usage, "build a site")
            source = self.resolve(scope, path)
            if not (source / "zola.toml").is_file():
                raise SandboxError(
                    f"'{path}' has no zola.toml, so it isn't a Zola site"
                )
            url = self.public_url(scope.workspace, slug)
            try:
                args = await asyncio.to_thread(self.prepare_build, name, scope, run_dir)
                async with self._runs:
                    exit_code, out, err, timed_out = await self.podman(
                        [*args, "python", "/sandbox/sitebuild.py", path, url],
                        BUILD_TIMEOUT,
                        name,
                    )
                if timed_out:
                    raise SandboxError(
                        f"the build took over {BUILD_TIMEOUT} s and was stopped"
                    )
                if exit_code != 0:
                    raise SandboxError(
                        f"the site didn't build: {(err or out).strip()[-2000:]}"
                    )
                files = await asyncio.to_thread(
                    self.stage_files, scope, run_dir / "out" / "site", slug
                )
            finally:
                await self.podman(["rm", "-f", "--ignore", name], 60, None)
                await asyncio.to_thread(shutil.rmtree, run_dir, True)
            published = await asyncio.to_thread(self.page_changes, scope, {slug})
        log.info(
            "built %s from %s for %s: %d files", slug, path, scope.workspace, files
        )
        return {
            "slug": slug,
            "url": f"{url}/",
            "files": files,
            "zola": (out + err).strip()[-500:],  # zola reports on stderr
            "published": published,
        }

    async def op_build_system_site(self, site: str) -> dict[str, Any]:
        """Build a system site whose repo zola.toml names a theme with [extra.build]
        theme_from into the pages site's `.<site>.new`, for sites.build to mark and swap in.
        Answers with where it went; raises with zola's error if it didn't build."""
        if not isinstance(site, str) or not SLUG_RE.fullmatch(site):
            raise SandboxError(f"bad site '{site}'")
        source = self.config.sites_source / site
        try:
            conf = tomllib.loads((source / "zola.toml").read_text())
        except FileNotFoundError:
            raise SandboxError(f"there's no system site '{site}'") from None
        origin = conf.get("extra", {}).get("build", {}).get("theme_from")
        if not origin:
            raise SandboxError(
                f"{site}'s zola.toml names no [extra.build] theme_from; it builds on the host"
            )
        if origin != "system":
            # A system site pins a workspace's theme rather than following its live folder
            # (docs/.proposals/shared-sites.md, Decision 2), and pinning isn't built yet.
            raise SandboxError(
                f"{site} takes its theme from {origin!r}, but a system site can only use "
                "'system' until workspace themes can be pinned"
            )
        name = f"sandbox-{secrets.token_hex(6)}"
        run_dir = self.config.scripts / name
        new = self.config.site_dir / f".{site}.new"
        async with self.lock(f"site:{site}"):  # no workspace can be called that
            try:
                args = await asyncio.to_thread(
                    self.prepare_system_build, name, site, run_dir
                )
                async with self._runs:
                    exit_code, out, err, timed_out = await self.podman(
                        [
                            *args,
                            "python",
                            "/sandbox/sitebuild.py",
                            "/site",
                            f"{self.config.site_url.rstrip('/')}/{site}",
                            "/entries",
                        ],
                        SYSTEM_BUILD_TIMEOUT,
                        name,
                    )
                if timed_out:
                    raise SandboxError(
                        f"the build of {site} took over {SYSTEM_BUILD_TIMEOUT} s and was stopped"
                    )
                if exit_code != 0:
                    raise SandboxError(
                        f"zola build failed for {site}:\n{(err or out).strip()[-2000:]}"
                    )
                files = await asyncio.to_thread(
                    self.copy_out, run_dir / "out" / "site", new
                )
            finally:
                await self.podman(["rm", "-f", "--ignore", name], 60, None)
                await asyncio.to_thread(shutil.rmtree, run_dir, True)
        log.info("built system site %s in the sandbox: %d files", site, files)
        return {"site": site, "path": str(new), "files": files}

    def prepare_system_build(self, name: str, site: str, run_dir: Path) -> list[str]:
        """A system site build's podman arguments: no network, its repo source, a copy of its
        entries and the repo's themes read-only, an empty /out. No workspace's /shared: a
        system site's theme is the repo's (theme_from = "system").

        The entries are copied, not mounted: the sites and research containers can write
        pages/entries, and could make the site's folder a symlink that podman would mount
        wherever it points. The copy follows none, at any depth."""
        (run_dir / "code").mkdir(parents=True)
        (run_dir / "out").mkdir()
        entries = run_dir / "entries"
        try:
            with safefs.folder(self.config.sites_content, (site,)) as d:
                safefs.copy_tree(d, entries, ENTRIES_BYTES)
        except FileNotFoundError:
            entries.mkdir(exist_ok=True)
        except OSError as e:
            raise SandboxError(f"couldn't read {site}'s entries: {e}") from None
        shutil.copyfile(SITEBUILD, run_dir / "code" / "sitebuild.py")
        return [
            *self.hardening(name),
            "--network",
            "none",
            "-v",
            f"{self.config.sites_source / site}:/site:{DATA_RO}",
            "-v",
            f"{entries}:/entries:{DATA_RO}",
            "-v",
            f"{self.config.system_themes}:/system/themes:{DATA_RO}",
            "-v",
            f"{run_dir / 'out'}:/out:rw,noexec,nosuid,nodev",
            "-v",
            f"{run_dir / 'code'}:/sandbox:ro",
            IMAGE,
        ]

    def copy_out(self, source: Path, dest: Path) -> int:
        """A system site's built files into `dest` (replaced), plain files only.

        `dest` is in the pages site, which the sites and research containers can write as
        this same user while the copy runs. So nothing here follows a symlink: each folder
        is made and opened relative to its parent's open fd, never by path, and each file
        is created new (O_EXCL), so a symlink planted on the way stops the copy instead of
        sending a write outside the pages site."""
        files, size = regular_files(source) if source.is_dir() else ([], 0)
        if not files:
            raise SandboxError("the build produced no files")
        if size > PUBLISH_MAX_BYTES:
            raise SandboxError(f"the built site is {size >> 20} MB, over the cap")
        remove_path(dest)
        folders: dict[tuple[str, ...], int] = {}
        try:
            parent = open_dir(dest.parent)
            try:
                folders[()] = make_dir(parent, dest.name)
            finally:
                os.close(parent)
            for src, rel in files:
                *parts, name = Path(rel).parts
                for i in range(len(parts)):
                    key = tuple(parts[: i + 1])
                    if key not in folders:
                        folders[key] = make_dir(folders[key[:-1]], parts[i])
                copy_into(src, folders[tuple(parts)], name)
        except OSError as e:
            raise SandboxError(
                f"couldn't copy the built site into {dest.name}: {e.strerror or e}"
            ) from None
        finally:
            for fd in folders.values():
                os.close(fd)
        return len(files)

    def prepare_build(self, name: str, scope: Scope, run_dir: Path) -> list[str]:
        """A build container's podman arguments: no network, the workspace's own folders
        (but /public) and everything else read-only, an empty /out, and the helper."""
        (run_dir / "code").mkdir(parents=True)
        (run_dir / "out").mkdir()
        shutil.copyfile(SITEBUILD, run_dir / "code" / "sitebuild.py")
        return [
            *self.hardening(name),
            "--network",
            "none",
            *(
                a
                for mount, root in scope.roots.items()
                if mount != "/public"
                for a in ("-v", f"{root}:{mount}:{DATA_RO}")
            ),
            *self.read_only_mounts(scope.workspace),
            "-v",
            f"{run_dir / 'out'}:/out:rw,noexec,nosuid,nodev",
            "-v",
            f"{run_dir / 'code'}:/sandbox:ro",
            IMAGE,
        ]

    def others_shared(self, workspace: str) -> list[tuple[str, Path]]:
        """Every other workspace's shared folder, as (workspace, folder): workspaces that
        have used the sandbox, so their folders exist."""
        return sorted(
            (d.name, d / "shared")
            for d in self.config.root.iterdir()
            if d.name != workspace
            and KEY_RE.fullmatch(d.name)
            and not d.is_symlink()
            and (d / "shared").is_dir()
            and not (d / "shared").is_symlink()
        )

    # --- files ---

    async def op_write(
        self, scope: dict[str, Any], path: str, content: str = "", delete: bool = False
    ) -> dict[str, Any]:
        """Write a text file, or delete a file or folder (always allowed, so a workspace over
        its limit can get back under). Deleting exactly one of the workspace's mounts empties
        it."""
        s = self.scope(scope)
        mount = self.split(s, path)[0]
        self.idle(s.workspace)
        async with self.lock(s.workspace):
            result = await self.write(s, path, content, delete)
            if mount == "/public" and (target := self.split(s, path)[2]) != s.public:
                result["published"] = await asyncio.to_thread(
                    self.page_changes, s, {target.relative_to(s.public).parts[0]}
                )
        return result

    async def write(
        self, s: Scope, path: str, content: str, delete: bool
    ) -> dict[str, Any]:
        """op_write's work, under the workspace's lock."""
        if delete:
            return await asyncio.to_thread(self.delete, s, path)
        data = (content or "").encode()
        if len(data) > WRITE_BYTES:
            raise SandboxError(
                f"content is {len(data)} bytes; the limit is {WRITE_BYTES}"
            )
        usage = await asyncio.to_thread(snapshot, s)
        if usage.total + len(data) > WORKSPACE_MAX_BYTES:
            raise self.over_quota(usage, "write files")
        target = self.resolve(s, path)
        await asyncio.to_thread(write_regular, target, data, path)
        return {"path": path.strip(), "bytes": len(data)}

    def delete(self, scope: Scope, path: str) -> dict[str, Any]:
        """Only the parent is resolved: a symlink is removed itself, never what it points to."""
        mount, root, target = self.split(scope, path)
        if target == root:
            if path.strip() != mount:
                raise SandboxError(
                    "give the path of the file or folder to delete, or one of "
                    f"{', '.join(scope.roots)} to empty it"
                )
            for child in list(root.iterdir()):
                remove_path(child)
            return {"path": mount, "emptied": True}
        parent = target.parent.resolve()
        if not parent.is_relative_to(root.resolve()):
            raise SandboxError(f"'{path}' points outside {mount}")
        target = parent / target.name
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            raise SandboxError(f"there's no '{path}'") from None
        remove_path(target)
        return {"path": path.strip(), "folder": stat.S_ISDIR(st.st_mode)}

    # --- pages ---

    def public_url(self, workspace: str, path: str = "") -> str:
        """Where `path` in a workspace's /public is on the workspace pages site."""
        return f"{self.config.public_url.rstrip('/')}/{workspace}/{path}"

    def page_url(self, workspace: str, item: Path) -> str:
        """A top-level entry of /public on the site: a folder as itself, a file as the file."""
        return self.public_url(
            workspace, quote(item.name) + ("/" if item.is_dir() else "")
        )

    def blocked(self, item: Path) -> list[str]:
        """What in a page's HTML the site's CSP blocks, so the agent hears it won't run."""
        origin = "/".join(self.config.public_url.split("/")[:3])
        files = [
            f for f, name in regular_files(item)[0] if name.endswith((".html", ".htm"))
        ]
        found = set()
        for f in files[:CSP_SCAN]:
            html = safefs.read_regular(f.parent, (f.name,), WRITE_BYTES)
            if html is not None:
                found.update(csp_blocked(html.decode(errors="replace"), origin))
        return sorted(found)

    def page_changes(self, scope: Scope, names: set[str]) -> dict[str, Any] | None:
        """Where the top-level entries `names` of /public that changed are now, with what
        their CSP blocks, and which of them are gone; None when there are none. Hidden
        entries are left out: the site doesn't serve them."""
        live, removed = [], []
        for name in sorted(n for n in names if not n.startswith(".")):
            item = scope.public / name
            if not os.path.lexists(item):
                removed.append(name)
            elif not item.is_symlink() and (item.is_dir() or item.is_file()):
                live.append(
                    {
                        "slug": name,
                        "url": self.page_url(scope.workspace, item),
                        "blocked": self.blocked(item),
                    }
                )
        result = {"live": live, "removed": removed}
        return {k: v for k, v in result.items() if v} or None

    async def op_publish(
        self,
        scope: dict[str, Any],
        slug: str = "",
        path: str = "",
        remove: bool = False,
    ) -> dict[str, Any]:
        """A page's address and link card. /public is the workspace's pages, live as they're
        written, so nothing needs publishing; with `path` outside /public, that file or
        folder is first copied to /public/<slug> (an HTML file as its index.html), and with
        `remove`, /public's entry for the page is deleted. Without a slug or path, the
        workspace's pages."""
        s = self.scope(scope)
        path = (path or "").strip()
        outside = bool(path) and self.split(s, path)[0] != "/public"
        if (outside or (slug and not path)) and not (
            isinstance(slug, str) and SLUG_RE.fullmatch(slug)
        ):
            raise SandboxError(
                "slug must be 1-63 lowercase letters, digits or hyphens, not starting "
                "or ending with a hyphen, e.g. 'trip-plan'"
            )
        self.idle(s.workspace)
        async with self.lock(s.workspace):
            if outside:
                await asyncio.to_thread(self.stage, s, path, slug)
                name = slug
            elif path:
                target = self.split(s, path)[2]
                if target == s.public:
                    raise SandboxError(
                        "give a page in /public, e.g. /public/trip-plan, not all of it"
                    )
                name = target.relative_to(s.public).parts[0]
            elif slug:
                entries = self.public_entries(s.public, slug)
                name = entries[0].name if entries else slug
            else:
                return await asyncio.to_thread(self.listing, s)
            item = s.public / name
            if remove:
                entries = self.public_entries(s.public, Path(name).stem) or (
                    [item] if os.path.lexists(item) else []
                )
                if not entries:
                    raise SandboxError(f"there's no page '{name}' in /public")
                for e in entries:
                    await asyncio.to_thread(remove_path, e)
                return {"slug": name, "removed": True}
            if not (item.is_dir() or item.is_file()) or item.is_symlink():
                raise SandboxError(
                    f"there's nothing at /public/{name} to publish; give the path of "
                    "the file or folder to publish"
                )
            return await asyncio.to_thread(self.page_info, s.workspace, item)

    def listing(self, scope: Scope) -> dict[str, Any]:
        """The workspace's pages: /public's top-level entries and where they are."""
        items = sorted(e for e in scope.public.iterdir() if not e.name.startswith("."))
        return {
            "site": self.public_url(scope.workspace),
            "pages": [
                {"slug": e.name, "url": self.page_url(scope.workspace, e)}
                for e in items[:LIST_MAX]
            ],
        }

    def page_info(self, workspace: str, item: Path) -> dict[str, Any]:
        """A page's address, size, what its CSP blocks and its link card."""
        url = self.page_url(workspace, item)
        # Read without following a symlink a run left: index.html could point at another
        # workspace's files, whose title and description would go on the card.
        entry = (item, "index.html") if item.is_dir() else (item.parent, item.name)
        title, description = item.name, ""
        data = None
        if entry[1].lower().endswith((".html", ".htm")):
            data = safefs.read_regular(entry[0], (entry[1],), WRITE_BYTES)
        if data is not None:
            html = data.decode(errors="replace")
            if m := TITLE_RE.search(html):
                title = htmllib.unescape(" ".join(m.group(1).split())) or title
            description = page_description(html)
        return {
            "slug": item.name,
            "url": url,
            "files": len(regular_files(item)[0]),
            "blocked": self.blocked(item),
            "card": chatimage.card.make(
                self.config.site_dir,
                url,
                title,
                f"Pages · {workspace}",
                description,
                images=self.config.site_url,
            ),
        }

    def public_entries(self, public: Path, slug: str) -> list[Path]:
        """/public's entries for `slug`: its folder, or a file named <slug>.<ext>."""
        if not public.is_dir():
            return []
        return [
            e
            for e in public.iterdir()
            if e.name == slug or (Path(e.name).stem == slug and "." in e.name)
        ]

    def stage(self, scope: Scope, path: str, slug: str) -> None:
        """Copy a file or folder from the workspace's own folders to /public/<slug>, in place
        of whatever was there: an HTML file as its index.html, a folder whole (plain files
        only)."""
        source = self.resolve(scope, path)
        if not source.exists():
            raise SandboxError(f"there's no '{path}'")
        files, size = regular_files(source)
        if not files:
            raise SandboxError(f"'{path}' has no files to publish")
        if size > PUBLISH_MAX_BYTES:
            raise SandboxError(
                f"'{path}' is {size >> 20} MB; the most publish copies is {PUBLISH_MAX_BYTES >> 20} MB"
            )
        self.put_public(scope, files, slug)

    def stage_files(self, scope: Scope, source: Path, slug: str) -> int:
        """A build's output into /public/<slug>: its plain files, within the copy cap."""
        files, size = regular_files(source) if source.is_dir() else ([], 0)
        if not files:
            raise SandboxError("the build produced no files")
        if size > PUBLISH_MAX_BYTES:
            raise SandboxError(
                f"the built site is {size >> 20} MB; a build can copy at most "
                f"{PUBLISH_MAX_BYTES >> 20} MB"
            )
        self.put_public(scope, files, slug)
        return len(files)

    def put_public(
        self, scope: Scope, files: list[tuple[Path, str]], slug: str
    ) -> None:
        """Copy plain files into /public/<slug>, in place of whatever was there for it, built
        in a hidden folder beside it (which the site doesn't serve) and swapped in."""
        new = Path(tempfile.mkdtemp(dir=scope.public, prefix=f".{slug}."))
        try:
            for src, name in files:
                (new / name).parent.mkdir(parents=True, exist_ok=True)
                copy_regular(src, new / name)
            new.chmod(0o755)
            for old in self.public_entries(scope.public, slug):
                remove_path(old)
            os.rename(new, scope.public / slug)
        except BaseException:
            remove_path(new)
            raise

    # --- server ---

    async def cleanup(self) -> None:
        """Remove what an earlier runner left mid-run: its containers and script folders."""
        await self.podman(["rm", "-f", "--filter", f"label={LABEL}"], 120, None)
        await asyncio.to_thread(shutil.rmtree, self.config.scripts, True)

    async def gc_loop(self) -> None:
        while True:
            if removed := await asyncio.to_thread(self.gc):
                log.info("removed idle threads' /work: %s", ", ".join(removed))
            await asyncio.sleep(3600)


class SystemBuilds(hostrpc.Service):
    """The runner's build_system_site alone (and ping), for SANDBOX_BUILD_SOCKET: what the
    sites and research containers may ask of the sandbox. No op here takes a scope."""

    log = log

    def __init__(self, runner: Runner):
        super().__init__()
        self.runner = runner

    async def op_build_system_site(self, site: str) -> dict[str, Any]:
        return await self.runner.op_build_system_site(site)


async def serve(config: Config, stop: asyncio.Event | None = None) -> None:
    """Serve the runner on its socket, and SystemBuilds on the build socket, until `stop`
    is set, or without one until SIGTERM."""
    runner = Runner(config)
    await runner.cleanup()
    config.root.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    on_sigterm = stop is None
    if stop is None:
        stop = asyncio.Event()
        loop.add_signal_handler(signal.SIGTERM, stop.set)
    gc = asyncio.create_task(runner.gc_loop())
    servers = [hostrpc.serve(runner, config.socket, limit=LIMIT, stop=stop)]
    if config.build_socket is not None:
        servers.append(
            hostrpc.serve(SystemBuilds(runner), config.build_socket, stop=stop)
        )
    try:
        await asyncio.gather(*servers)
    finally:
        stop.set()  # one failed: the other stops too
        gc.cancel()
        if on_sigterm:
            loop.remove_signal_handler(signal.SIGTERM)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(serve(Config.from_env()))
