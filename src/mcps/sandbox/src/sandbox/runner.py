"""The sandbox runner: a host daemon that runs the agent's code in throwaway podman containers
and publishes what it makes to the pages site.

Each run gets a fresh container from the sandbox image with no network except the
allowlisting proxy on sandbox-net, a read-only root, CPU/memory/process limits and a
time limit. What it can see depends on where the call came from, which the skills in
AnythingLLM pass as a scope of {workspace, thread} (never chosen by the model):

  /work     the thread's scratch folder, deleted a week after the thread last used it
  /project  the workspace's folder, shared by its threads and kept (pip installs go here)
  /pages    the workspace's published pages, read-only

A workspace's folders together are held to WORKSPACE_MAX_BYTES. The script itself is
mounted read-only from a host-only folder at /sandbox.

Publishing copies a file or folder to `<site>/<slug>/`. The slug's `.page` marker holds the
workspace that owns it: only that workspace can replace or remove it, and the pages site
lets a marked folder use inline CSS (host/caddy/pages.Caddyfile). The site's root
`index.html` lists every page and is rewritten after each change.

Config (environment):
  ANYTHINGLLM_STORAGE, PUBLIC_HOST
                    this machine's storage directory and tailnet name, from host.env
                    (default /srv/anythingllm/storage, and no name: links use 127.0.0.1)
  SANDBOX_SOCKET    the Unix socket to listen on (default <storage>/sandbox/runner.sock)
  SANDBOX_ROOT      workspace folders, host-only (default ~/.local/share/everythingllm/sandbox);
                    run scripts go in its `.runs` folder
  SANDBOX_SITE_DIR  the pages site's root, where pages are published (default <storage>/site)
  SANDBOX_SITE_URL  public URL of SANDBOX_SITE_DIR (default https://<PUBLIC_HOST>:8445/)
"""

from __future__ import annotations

import asyncio
import html as htmllib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import hostrpc
import linkcard

log = logging.getLogger("sandbox-runner")
# Big enough for a run's output, which the runner caps well below this.
LIMIT = 8 * 1024 * 1024


class SandboxError(hostrpc.RunnerError):
    """An error to show the agent: bad arguments, a missing file, the runner being down."""


# Also named in the Makefile's sandbox-setup and host/systemd/sandbox-proxy.service; the proxy's
# address (10.89.77.2:8888) is set in Containerfile.sandbox, that unit and tinyproxy.conf.
IMAGE = "localhost/everythingllm-sandbox"
NETWORK = "sandbox-net"
PROXY_CONTAINER = "sandbox-proxy"
LABEL = "everythingllm-sandbox=1"

LANGUAGES = {"python": ("main.py", "python"), "bash": ("main.sh", "bash")}
KEY_RE = re.compile(r"^[a-z0-9_][a-z0-9_-]{0,99}$")  # workspace slugs and thread ids
SLUG_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)  # as sites.store.NAME_RE
MOUNTS = ("/work", "/project")
MEMORY = "1g"
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 300
MAX_PARALLEL = 2
OUTPUT_BYTES = 20_000  # per stream of a run
WRITE_BYTES = 1_000_000  # op_write
WORKSPACE_WARN_BYTES = 4 << 30
WORKSPACE_MAX_BYTES = 5 << 30  # no new runs or writes past this; deletes still work
PUBLISH_MAX_BYTES = 500 << 20
THREAD_MAX_AGE = 7 * 24 * 3600
LIST_MAX = 200  # files named in a run's changed list
# A request answers within WAIT; a run that's still going carries on, and the skill waits
# on it again with op_wait.
WAIT = 45
RESULT_KEEP = 3600  # how long a finished run's result can still be fetched
PAGE_MARKER = ".page"  # JSON: the owning workspace, title and entry file; the pages site allows its inline CSS

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
    site_dir: Path
    site_url: str

    @property
    def scripts(self) -> Path:
        return self.root / ".runs"

    @classmethod
    def from_env(cls) -> Config:
        get = os.environ.get
        storage = hostrpc.storage()
        host = get("PUBLIC_HOST")
        return cls(
            socket=hostrpc.socket_path("sandbox", "SANDBOX_SOCKET"),
            root=Path(
                get("SANDBOX_ROOT", "~/.local/share/everythingllm/sandbox")
            ).expanduser(),
            site_dir=Path(get("SANDBOX_SITE_DIR", storage / "site")),
            site_url=get(
                "SANDBOX_SITE_URL",
                f"https://{host}:8445/" if host else "http://127.0.0.1:8445/",
            ),
        )


@dataclass(frozen=True)
class Scope:
    """Where a call came from, and the host folders behind its mounts: the workspace's
    folder (`home`) holds project/ (/project) and threads/<thread>/ (/work)."""

    workspace: str
    thread: str
    home: Path

    @property
    def roots(self) -> dict[str, Path]:
        return {
            "/work": self.home / "threads" / self.thread,
            "/project": self.home / "project",
        }

    def mount_of(self, parts: tuple[str, ...]) -> tuple[str, tuple[str, ...]] | None:
        """A path under `home`, as parts, as its mount and its parts inside that; None
        outside both (another thread's /work)."""
        for mount, root in self.roots.items():
            prefix = root.relative_to(self.home).parts
            if parts[: len(prefix)] == prefix:
                return mount, parts[len(prefix) :]
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


def snapshot(scope: Scope) -> Usage:
    """Every visible file under /work and /project by its path in the sandbox, and the size
    of the whole workspace (its other threads too). Hidden top-level entries (.local with
    pip installs, .cache…) count toward the size only."""
    usage = Usage()
    for dirpath, _, filenames in os.walk(scope.home):
        where = scope.mount_of(Path(dirpath).relative_to(scope.home).parts)
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


def csp_blocked(html: str) -> list[str]:
    """What in a page the site's CSP will block: [] when nothing is."""
    return [what for what, pattern in _CSP_BLOCKED if pattern.search(html)]


def marker(folder: Path) -> dict[str, str] | None:
    """A published page's marker; None for anything that isn't one (a Zola site, the
    podcasts, a folder nobody marked)."""
    file = folder / PAGE_MARKER
    if (
        folder.is_symlink()
        or not folder.is_dir()
        or file.is_symlink()
        or not file.is_file()
    ):
        return None
    try:
        m = json.loads(file.read_text())
    except ValueError:
        return None
    return m if isinstance(m, dict) and m.get("workspace") else None


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
    """Every plain file under `source` with its path inside it, and their size; symlinks
    and anything else that isn't a file or folder are left out."""
    if source.is_file():
        name = (
            "index.html" if source.suffix.lower() in (".html", ".htm") else source.name
        )
        return [(source, name)], source.stat().st_size
    found, size = [], 0
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = [
            d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))
        ]
        for name in filenames:
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
    _site: asyncio.Lock = field(default_factory=asyncio.Lock)  # who owns which slug

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
        s = Scope(workspace, thread, self.config.root / workspace)
        for d in s.roots.values():
            d.mkdir(parents=True, exist_ok=True)
        os.utime(s.roots["/work"])
        return s

    def split(self, scope: Scope, path: str) -> tuple[str, Path, Path]:
        """`path` in the sandbox (`/work/…`, `/project/…`, or relative to /work) as its mount,
        that mount's host folder and the path inside it, unresolved. `..` is refused."""
        path = path.strip()
        mount = next((m for m in MOUNTS if path == m or path.startswith(m + "/")), None)
        if mount is None and path.startswith("/"):
            raise SandboxError(f"bad path '{path}': use a path under /work or /project")
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
            problems.append(f"image {IMAGE} is missing (make sandbox-setup)")
        if network[0] != 0:
            problems.append(f"network {NETWORK} is missing (make sandbox-setup)")
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
            "warning": (
                f"this workspace's sandbox uses {after.total >> 20} MB of its {WORKSPACE_MAX_BYTES >> 20} MB; "
                "delete what isn't needed"
            )
            if after.total > WORKSPACE_WARN_BYTES
            else None,
        }

    def prepare(
        self, name: str, scope: Scope, run_dir: Path, script: str, code: str
    ) -> list[str]:
        """Write the run's script and return its podman arguments. Each of the workspace's
        pages is bound read-only onto an empty folder of its name under the run's own
        read-only /pages, so a run sees its pages and nothing else there."""
        (run_dir / "code").mkdir(parents=True)
        (run_dir / "code" / script).write_text(code)
        pages = run_dir / "pages"
        pages.mkdir()
        binds = []
        for slug, folder in self.pages(scope.workspace):
            (pages / slug).mkdir()
            binds += ["-v", f"{folder}:/pages/{slug}:ro"]
        # No --rm: the container is kept until execute has asked podman whether it ran out of memory.
        return [
            "run",
            "--name",
            name,
            "--label",
            LABEL,
            "--network",
            NETWORK,
            "--dns",
            "none",
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
            *(
                a
                for mount, root in scope.roots.items()
                for a in ("-v", f"{root}:{mount}")
            ),
            "-v",
            f"{run_dir / 'code'}:/sandbox:ro",
            "-v",
            f"{pages}:/pages:ro",
            *binds,
            IMAGE,
        ]

    # --- files ---

    async def op_write(
        self, scope: dict[str, Any], path: str, content: str = "", delete: bool = False
    ) -> dict[str, Any]:
        """Write a text file, or delete a file or folder (always allowed, so a workspace over
        its limit can get back under). Deleting exactly /work or /project empties it."""
        s = self.scope(scope)
        self.idle(s.workspace)
        async with self.lock(s.workspace):
            if delete:
                return await asyncio.to_thread(self.delete, s, path)
            data = (content or "").encode()
            if len(data) > WRITE_BYTES:
                raise SandboxError(
                    f"content is {len(data)} bytes; the limit is {WRITE_BYTES}"
                )
            if (usage := await asyncio.to_thread(snapshot, s)).total + len(
                data
            ) > WORKSPACE_MAX_BYTES:
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
                    "give the path of the file or folder to delete, or /work or /project to empty it"
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

    def pages(self, workspace: str) -> list[tuple[str, Path]]:
        """The workspace's published pages, as (slug, folder)."""
        site = self.config.site_dir
        if not site.is_dir():
            return []
        return sorted(
            (d.name, d)
            for d in site.iterdir()
            if SLUG_RE.fullmatch(d.name)
            and (marker(d) or {}).get("workspace") == workspace
        )

    async def op_publish(
        self, scope: dict[str, Any], slug: str, path: str = "", remove: bool = False
    ) -> dict[str, Any]:
        """Publish a file or folder as `/<slug>/` (an HTML file becomes its index.html, a
        folder is copied whole), replacing what the workspace had there; or remove the slug."""
        s = self.scope(scope)
        if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
            raise SandboxError(
                "slug must be 1-63 lowercase letters, digits or hyphens, not starting "
                "or ending with a hyphen, e.g. 'trip-plan'"
            )
        dest = self.config.site_dir / slug
        self.idle(s.workspace)
        async with self.lock(s.workspace), self._site:
            if (
                os.path.lexists(dest)
                and (marker(dest) or {}).get("workspace") != s.workspace
            ):
                raise SandboxError(
                    f"'{slug}' is taken on the pages site; choose another slug"
                )
            if remove:
                if not os.path.lexists(dest):
                    raise SandboxError(f"there's no page '{slug}'")
                m = marker(dest) or {}
                await asyncio.to_thread(remove_path, dest)
                linkcard.remove(
                    self.config.site_dir,
                    self.page_url(slug, m.get("entry", "index.html")),
                )
                result: dict[str, Any] = {"slug": slug, "removed": True}
            else:
                if not path or not path.strip():
                    raise SandboxError("give the path of the file or folder to publish")
                source = self.resolve(s, path)
                if not source.exists():
                    raise SandboxError(f"there's no '{path}'")
                files, size = await asyncio.to_thread(regular_files, source)
                if not files:
                    raise SandboxError(f"'{path}' has no files to publish")
                if size > PUBLISH_MAX_BYTES:
                    raise SandboxError(
                        f"'{path}' is {size >> 20} MB; the most publish copies is {PUBLISH_MAX_BYTES >> 20} MB"
                    )
                entry, blocked = await asyncio.to_thread(
                    self.replace, dest, files, s.workspace
                )
                url = self.page_url(slug, entry)
                result = {
                    "slug": slug,
                    "url": url,
                    "files": len(files),
                    "blocked": blocked,
                    "card": await asyncio.to_thread(self.card, dest, url, s.workspace),
                }
            await asyncio.to_thread(self.rebuild_index)
        return result

    def page_url(self, slug: str, entry: str) -> str:
        return f"{self.config.site_url.rstrip('/')}/{page_path(slug, entry)}"

    def card(self, page: Path, url: str, workspace: str) -> str:
        """The chat's link card for a page just published (see linkcard); "" when it
        couldn't be made."""
        m = marker(page) or {}
        entry = m.get("entry", "index.html")
        title, description = entry, ""  # a file that isn't a page goes by its name
        if entry.endswith((".html", ".htm")):
            title = m.get("title") or page.name
            description = page_description((page / entry).read_text(errors="replace"))
        return linkcard.make(
            self.config.site_dir,
            url,
            title,
            f"Pages · {workspace}",
            description,
        )

    def replace(
        self, dest: Path, files: list[tuple[Path, str]], workspace: str
    ) -> tuple[str, list[str]]:
        """Build the page in a temp folder beside `dest` and swap it in, so the site never
        serves half of one. Returns its entry file and what in its HTML the CSP blocks."""
        site = self.config.site_dir
        names = [name for _, name in files]
        entry = "index.html" if "index.html" in names else names[0]
        new = Path(tempfile.mkdtemp(dir=site, prefix=f".{dest.name}."))
        try:
            for source, name in files:
                (new / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, new / name)
                (new / name).chmod(0o644)
            html = {
                n: (new / n).read_text(errors="replace")
                for n in names
                if n.endswith((".html", ".htm"))
            }
            m = TITLE_RE.search(html.get(entry, ""))
            title = htmllib.unescape(" ".join(m.group(1).split())) if m else dest.name
            (new / PAGE_MARKER).write_text(
                json.dumps({"workspace": workspace, "title": title, "entry": entry})
                + "\n"
            )
            new.chmod(0o755)
            old = None
            if os.path.lexists(dest):
                old = site / f".{dest.name}.old-{secrets.token_hex(3)}"
                os.rename(dest, old)
            os.rename(new, dest)
            if old:
                remove_path(old)
        except BaseException:
            remove_path(new)
            raise
        return entry, sorted({w for text in html.values() for w in csp_blocked(text)})

    def rebuild_index(self) -> None:
        """The site root's listing of every page, newest first, from the pages' markers."""
        site = self.config.site_dir
        rows = []
        for d in site.iterdir():
            if not SLUG_RE.fullmatch(d.name) or not (m := marker(d)):
                continue
            updated = datetime.fromtimestamp(
                (d / PAGE_MARKER).stat().st_mtime, timezone.utc
            )
            rows.append(
                (
                    updated,
                    (
                        f'<li><a href="{page_path(d.name, m.get("entry", "index.html"))}">'
                        f"{htmllib.escape(m.get('title') or d.name)}</a> "
                        f'<span>{htmllib.escape(m["workspace"])} · <time datetime="{updated.isoformat()}">'
                        f"{updated:%Y-%m-%d}</time></span></li>"
                    ),
                )
            )
        items = "\n".join(row for _, row in sorted(rows, reverse=True))
        body = f"<ul>\n{items}\n</ul>" if rows else "<p>Nothing published yet.</p>"
        (site / PAGE_MARKER).touch()  # the listing's own inline CSS
        hostrpc.atomic_write(
            site / "index.html",
            f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pages</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin: 0 auto; max-width: 46rem; padding: 2rem 1rem;
         font: 1rem/1.6 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  ul {{ padding: 0; list-style: none; }}
  li {{ padding: .4rem 0; border-bottom: 1px solid #8884; }}
  span {{ opacity: .6; font-size: .9em; margin-left: .5rem; }}
</style>
</head>
<body>
<h1>Pages</h1>
{body}
</body>
</html>
""",
        )

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


async def serve(config: Config) -> None:
    runner = Runner(config)
    await runner.cleanup()
    config.root.mkdir(parents=True, exist_ok=True)
    gc = asyncio.create_task(runner.gc_loop())
    try:
        await hostrpc.serve(runner, config.socket, limit=LIMIT)
    finally:
        gc.cancel()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(serve(Config.from_env()))
