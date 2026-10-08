"""A sandbox workspace: the caller's folders (Scope) and the paths into them, what they
hold (Usage) against the workspace's limits, the file helpers the runner uses on them, and
the runner's config (Config).

A scope is where a call came from, {workspace, thread} as the skills in AnythingLLM pass it
(never chosen by the model); its folders are made on first use. A gateway client's scope
says "gateway": true, and its workspace is a CLIENT_PREFIX one. Paths into a scope's folders
stay inside the caller's own (split, resolve), and the file helpers never follow a symlink
or open a FIFO a run could have left.

Config (environment):
  ANYTHINGLLM_STORAGE, PUBLIC_HOST
                    this machine's storage directory and HTTPS name, from host.env
                    (default /srv/anythingllm/storage; PUBLIC_HOST is required, since
                    egress.toml needs it to load)
  SANDBOX_SOCKET    the Unix socket to listen on (default <storage>/everythingllm/sandbox/runner.sock)
  SANDBOX_ROOT      workspace folders, host-only (default
                    ~/.local/share/everythingllm/sandbox/workspaces);
                    run scripts go in its `.runs` folder
  SANDBOX_SYSTEM_THEMES  the themes mounted at /system/themes (default the repo's
                         packages/sandbox/zola/themes)
  SANDBOX_PUBLIC    every workspace's /public, as <workspace>/ (default
                    ~/.local/share/everythingllm/sandbox/public)
  SANDBOX_PUBLIC_URL  public URL of SANDBOX_PUBLIC (default https://<PUBLIC_HOST>:8447/)
  SANDBOX_SITE_DIR  the pages site's root, where the link cards and shown images are
                    saved (default ~/.local/share/everythingllm/pages/public)
  SANDBOX_SITE_URL  public URL of SANDBOX_SITE_DIR (default https://<PUBLIC_HOST>:8445/)
  SANDBOX_UPLOADS   AnythingLLM's chat attachments, whose text a run gets in
                    /work/attachments (default <storage>/direct-uploads)
  SANDBOX_ACCESS    each workspace's web and model access (default
                    ~/.local/share/everythingllm/sandbox/access.json)
  ANYTHINGLLM_ENV   AnythingLLM's .env, for the model keys (default <storage>/.env)
  USER_TIMEZONE     whose day a model budget is (sandbox.models)
  APPS_PORT         the apps server's port (sandbox.appsweb; default 8455)
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import hostenv
from egress import config as egress_config

from sandbox import appsweb
from sandbox.errors import SandboxError
from sandbox.names import KEY, SLUG

log = logging.getLogger("sandbox-runner")

PROFILE = "sandbox"  # egress.toml's profile, whose addresses the runs take
WEB_PROFILE = "sandbox-web"  # its profile for the runs of a workspace with web access
KEY_RE = re.compile(f"^{KEY}$")
# The MCP gateway's clients' workspaces (gateway.sandbox): kept for scopes that say
# "gateway": true, which AnythingLLM's skills never do, so a workspace someone happens to
# name "Client X" can't share a gateway client's folders.
CLIENT_PREFIX = "client-"
SLUG_RE = re.compile(f"^{SLUG}$")
REPO = Path(__file__).resolve().parents[4]  # <repo>/packages/sandbox/src/sandbox/
SYSTEM_THEMES = REPO / "packages" / "sandbox" / "zola" / "themes"  # the repo's themes
WORKSPACE_WARN_BYTES = 4 << 30
WORKSPACE_MAX_BYTES = 5 << 30  # no new runs or writes past this; deletes still work
# A run is stopped if, while it runs, the workspace grows past WORKSPACE_MAX_BYTES and
# RUN_SLACK, or holds more than MAX_FILES files and folders (hidden ones too): looked at
# every WATCH_SECONDS (sandbox.runner), and no one file it writes can be over
# FILE_MAX_BYTES (RLIMIT_FSIZE; sandbox.containers).
RUN_SLACK = 1 << 30
MAX_FILES = 200_000
THREAD_MAX_AGE = 7 * 24 * 3600


@dataclass
class Config:
    socket: Path
    root: Path
    system_themes: Path
    site_dir: Path
    site_url: str
    # egress-net, the sandbox profile's addresses (one slot each) and the proxy's URL; and
    # for a workspace with web access, its profile's and the proxy's public-only port.
    network: str = ""
    ips: tuple[str, ...] = ()
    proxy: str = ""
    web_ips: tuple[str, ...] = ()
    public_proxy: str = ""
    access_file: Path = Path("/nonexistent/access.json")  # SANDBOX_ACCESS
    # Model access: AnythingLLM's .env (the keys, read here, never in a run) and the log.
    model_env: Path = Path("/nonexistent/.env")
    model_log: Path = Path("/nonexistent/models")
    # Each run's model socket, in a folder of its own: a short path, as AF_UNIX's are.
    model_sockets: Path = Path("/nonexistent/m")
    # Each app's write-back token, host-only; the apps server's port (sandbox.appsweb),
    # with none none served.
    app_state: Path = Path("/nonexistent/apps")
    apps_port: int | None = None
    public_root: Path = Path("/nonexistent")
    public_url: str = "http://127.0.0.1:8447/"
    uploads: Path = Path("/nonexistent")  # AnythingLLM's direct-uploads

    @property
    def scripts(self) -> Path:
        return self.root / ".runs"

    @classmethod
    def from_env(cls) -> Config:
        get = os.environ.get
        egress = egress_config.load()  # ValueError without PUBLIC_HOST
        host = os.environ["PUBLIC_HOST"]
        return cls(
            socket=hostenv.socket_path("sandbox", "SANDBOX_SOCKET"),
            network=egress.network,
            ips=tuple(egress.profiles[PROFILE].ips.values()),
            proxy=egress.url,
            web_ips=tuple(
                egress.profiles[WEB_PROFILE].ips.values()
                if WEB_PROFILE in egress.profiles
                else ()
            ),
            public_proxy=egress.public_url,
            access_file=Path(
                get("SANDBOX_ACCESS", hostenv.data_dir() / "sandbox" / "access.json")
            ),
            model_env=Path(get("ANYTHINGLLM_ENV", hostenv.storage() / ".env")),
            model_log=hostenv.data_dir() / "sandbox" / "models",
            model_sockets=hostenv.data_dir() / "sandbox" / "m",
            app_state=hostenv.data_dir() / "sandbox" / "apps",
            apps_port=int(get("APPS_PORT") or appsweb.PORT),
            root=Path(
                get("SANDBOX_ROOT", hostenv.data_dir() / "sandbox" / "workspaces")
            ),
            system_themes=Path(get("SANDBOX_SYSTEM_THEMES", SYSTEM_THEMES)),
            site_dir=Path(get("SANDBOX_SITE_DIR", hostenv.site_dir())),
            site_url=get(
                "SANDBOX_SITE_URL",
                f"https://{host}:8445/",
            ),
            public_root=Path(
                get("SANDBOX_PUBLIC", hostenv.data_dir() / "sandbox" / "public")
            ),
            public_url=get(
                "SANDBOX_PUBLIC_URL",
                f"https://{host}:8447/",
            ),
            uploads=Path(get("SANDBOX_UPLOADS", hostenv.storage() / "direct-uploads")),
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
    gateway: bool = False  # a gateway client's: no chat behind it

    @property
    def roots(self) -> dict[str, Path]:
        return {
            "/work": self.home / "threads" / self.thread,
            "/project": self.home / "project",
            f"/shared/{self.workspace}": self.home / "shared",
            "/public": self.public,
        }


def make_scope(config: Config, scope: dict[str, Any]) -> Scope:
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
        config.root / workspace,
        config.public_root / workspace,
        gateway,
    )
    for d in s.roots.values():
        d.mkdir(parents=True, exist_ok=True)
    os.utime(s.roots["/work"])
    return s


def existing_scope(config: Config, workspace: str) -> Scope | None:
    """A workspace's scope as an address names it (its apps', sandbox.appsweb): None if it
    has no sandbox or is a gateway client's. Nothing is made."""
    if workspace.startswith(CLIENT_PREFIX) or not KEY_RE.fullmatch(workspace):
        return None
    s = Scope(
        workspace,
        "default",
        config.root / workspace,
        config.public_root / workspace,
    )
    return s if s.home.is_dir() else None


def split(scope: Scope, path: str) -> tuple[str, Path, Path]:
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


def resolve(scope: Scope, path: str) -> Path:
    """`path` after following symlinks, which must stay inside its mount's folder (not be
    all of it). Callers hold the workspace's lock, so no run can swap a link in after."""
    mount, root, target = split(scope, path)
    target, root = target.resolve(), root.resolve()
    if not target.is_relative_to(root):
        raise SandboxError(f"'{path}' points outside {mount}")
    if target == root:
        raise SandboxError(
            f"'{path}' is all of {mount}; give a file or folder inside it"
        )
    return target


@dataclass
class Usage:
    """What a workspace holds, from one walk of its folder."""

    files: dict[str, tuple[int, int]] = field(
        default_factory=dict
    )  # sandbox path: (mtime, size)
    total: int = 0
    count: int = 0  # every file and folder, hidden ones included: what MAX_FILES holds
    tops: dict[str, int] = field(
        default_factory=dict
    )  # size by top-level entry of each mount

    def biggest(self, n: int = 5) -> str:
        """For the error that stops a workspace over WORKSPACE_MAX_BYTES: no run can look then."""
        ranked = sorted(self.tops.items(), key=lambda kv: -kv[1])[:n]
        return ", ".join(f"{k} {v >> 20} MB" for k, v in ranked)


def snapshot(scope: Scope, files: bool = True) -> Usage:
    """Every visible file in the workspace's mounts by its path in the sandbox, and the size
    of the whole workspace (its other threads and its /public too). Hidden top-level entries
    (.local with pip installs, .cache…) count toward the size only. Without `files`, only
    the sizes and the count, for a check against the limits."""
    usage = Usage()
    roots = scope.roots
    for mount, root in roots.items():
        if not root.is_symlink():  # as os.walk from the folder above would leave it
            _walk_mount(usage, mount, os.fspath(root), files)
    # The rest of the workspace's folder: other chats' /work, and anything loose.
    mounted = {os.fspath(root) for root in roots.values()}
    for dirpath, dirnames, filenames in os.walk(scope.home):
        usage.count += len(dirnames) + len(filenames)
        dirnames[:] = [d for d in dirnames if os.path.join(dirpath, d) not in mounted]
        for name in filenames:
            try:
                size = os.lstat(os.path.join(dirpath, name)).st_size
            except FileNotFoundError:
                continue
            usage.total += size
            usage.tops["other chats' /work"] = (
                usage.tops.get("other chats' /work", 0) + size
            )
    return usage


def _walk_mount(usage: Usage, mount: str, root: str, files: bool) -> None:
    """snapshot's walk of one mount's folder, by its paths as strings: a Path for each of
    thousands of folders is most of the time a walk takes."""
    cut = len(root) + 1
    for dirpath, dirnames, filenames in os.walk(root):
        usage.count += len(dirnames) + len(filenames)
        inside = dirpath[cut:]  # "" in the mount's own folder
        top = inside.split(os.sep, 1)[0]
        for name in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except FileNotFoundError:
                continue
            usage.total += st.st_size
            key = f"{mount}/{top}/" if top else f"{mount}/{name}"
            usage.tops[key] = usage.tops.get(key, 0) + st.st_size
            if files and not (top or name).startswith("."):
                path = f"{mount}/{inside}/{name}" if inside else f"{mount}/{name}"
                usage.files[path] = (st.st_mtime_ns, st.st_size)


def public_changes(before: Usage, after: Usage) -> set[str]:
    """The top-level entries of /public a run added, changed or removed: its pages that changed."""
    b, a = (
        {p: sig for p, sig in u.files.items() if p.startswith("/public/")}
        for u in (before, after)
    )
    return {p.split("/")[2] for p in b.keys() | a.keys() if b.get(p) != a.get(p)}


def over_quota(usage: Usage, doing: str) -> SandboxError:
    return SandboxError(
        f"this workspace's sandbox uses {usage.total >> 20} MB, over its {WORKSPACE_MAX_BYTES >> 20} MB "
        f"limit, so it can't {doing}. Delete something with write-file (delete=true) first; "
        f"the biggest: {usage.biggest()}."
    )


def gc_threads(root: Path, now: float) -> list[str]:
    """Delete threads' /work folders (under the workspace folders in `root`) untouched for
    THREAD_MAX_AGE, a week, as of `now`; what went, as <workspace>/<thread>. /project stays."""
    cutoff = now - THREAD_MAX_AGE
    old = [
        d for d in root.glob("*/threads/*") if d.is_dir() and d.stat().st_mtime < cutoff
    ]
    for d in old:
        remove_path(d)
    return [f"{d.parent.parent.name}/{d.name}" for d in old]


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


def remove_path(path: Path) -> None:
    """Delete a file, symlink or folder; a symlink itself, never what it points to."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, onexc=force_remove)
    else:
        path.unlink(missing_ok=True)


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
