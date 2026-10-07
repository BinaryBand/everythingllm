"""browser-runner: one Chromium per AnythingLLM workspace, for the browse skills
(anythingllm/agent-skills/browse, browser-act, browser-read, browser-handoff,
browser-login), with a live card in the chat and a take-over view on its own HTTPS port.

A workspace's browser is a podman container (host/containers/browser) started on its first
call and stopped when nobody has used or watched it for IDLE seconds. Its profile (cookies,
logins, history) is the workspace's alone and outlives the container: it's in the
sandbox's folder for the workspace, beside the folders the sandbox mounts, never in one:

  <root>/<workspace>/browser/profile/   the profile, which no sandbox run can see
  <root>/<workspace>/project/downloads/ downloads, which run-code sees as /project/downloads

The container never mounts a folder a sandbox run can write: a run could make one a symlink,
and podman would mount wherever it points. Downloads land in <data>/downloads/<workspace>/
<thread>/ (the container's /downloads), and the runner copies each finished one to
/project/downloads after each of the thread's calls (collect_downloads), opening every step
without following a symlink (hostrpc.safefs), and says so in the thread's read.

Each chat thread has its own tab there; a scope of {workspace, thread} says which, and comes
from the skill's invocation, never the model. Gateway clients' `client-` workspaces have no
browser. The container is hardened like a service container (read-only root, every
capability dropped, keep-id, limits) and sits on egress-net at one of the browser profile's
addresses (egress.toml), so its only way out is the egress proxy's public port: public
hosts on 80 and 443, never the LAN, CGNAT (Tailscale's) or this machine. Chromium's own sandbox is
off (it needs namespaces the container doesn't give), so the container is the boundary.

The runner reaches the container through two Unix sockets in <data>/sockets/<slot>/: the
driver's (browser.driver, hostrpc) and x11vnc's, which the take-over view (browser.takeover)
carries over a WebSocket. Nothing in the container listens on the network.

Who has the browser: the agent, until the user takes over in the take-over view or the
agent hands it over (`handoff`, for a login, 2FA or a CAPTCHA, after which the agent ends
its reply so the card shows). While the user has it, the agent's actions and reads are refused. It
comes back when the user hands it back in the view, or says in the chat that they're done
(`handoff` with `done`), or when the browser is stopped.

What the card and the take-over view say of a tab (`state`): `working` while one of the
agent's ops for its chat runs and for ACTIVE seconds after (it thinks between steps),
`idle` once the agent holds it and does nothing with it, `waiting` while the agent waits
for the user (it handed the browser over, or waits for their OK or a login in that chat),
`user` when the user took it, and `closed`.

Saved logins (browser.vault, one vault per workspace) are the agent's to use and never to
read: it names a login and the fields, and the runner has the driver fill them on the
login's own site, over https. A login the user marked `ask` waits for their OK in the
take-over view (and on the card) before each use, good for GRANT minutes in that chat
alone. While the user has the browser, logins they send are offered for saving there too.

A passkey (`passkey`) is a saved entry too, asking first unless the user turns that off: the
driver puts it in the thread's page for the click that signs in with it. Only the user makes
one: while they have the browser, "Make a passkey" in the take-over view (make_passkeys)
lets every page make one, until one is made, browser.driver's MAKING_SECONDS pass or the
agent has it again. The runner saves what was made (save_made) as the view asks how things
stand, and as the browser goes back to the agent or stops, as a passkey made and not kept
is one the site has and nobody can use.

Without a saved login, the agent can ask for one (`ask_login`): a card in the chat links to
a page of its own on the take-over view's origin (/login/<id>/, browser.takeover) with a
form for the site of the chat's page, which only the runner names, and what the user sends
there goes into the vault. The request's id is the only way to the page, so it's long; it
outlives the browser (a save needs only the vault) until ASK_SECONDS pass.

Ops (each takes `scope`):
  open(url)                      go to url in the thread's tab -> {page, card, new}
  act(action, ref?, text?)       one browser.driver action -> {page}
  read(find?)                    the page as it is, or its lines with `find` -> {page}
  handoff(reason)                give the user the browser -> {card, takeover}
  handoff(done=true)             take it back -> {page}
  close()                        close the thread's tab
  logins()                       the workspace's saved logins, no secrets -> {logins, site}
  login(login, user_ref?, pass_ref?, submit?)  fill a saved login -> {page}, or
                                 {approval, card} while it waits for the user's OK
  code(login, ref, submit?)      fill a saved login's 2FA code -> as login
  passkey(login, ref)            click ref, the button that signs in with a passkey, with
                                 the saved passkey `login` in the page -> as login
  wait_approval(approval)        up to WAIT s -> {done, approved}
  ask_login()                    ask the user for a login for the thread's page's site
                                 -> {request, site, card}

`page` is browser.page's text; `card` the tab's live card line (browser.live), "" without
PUBLIC_HOST; `new` whether the card is new to this chat (the tab was just made).

Config (environment):
  ANYTHINGLLM_STORAGE, PUBLIC_HOST  from host.env (the egress profile needs PUBLIC_HOST)
  BROWSER_SOCKET         the socket to listen on (default <storage>/everythingllm/browser/runner.sock)
  BROWSER_ROOT           the workspaces' folders (default ~/.local/share/everythingllm/sandbox/workspaces,
                         the sandbox's SANDBOX_ROOT)
  BROWSER_DATA           the runner's own folder (default ~/.local/share/everythingllm/browser):
                         sockets/<slot>/, downloads/<workspace>/ and novnc/ (copied from the image by
                         hostctl browser-images)
  BROWSER_LIVE_PORT      the live cards' port (default 8453), on LIVE_HOST (default 127.0.0.1)
  BROWSER_TAKEOVER_PORT  the take-over view's port (default 8454), on 127.0.0.1
  BROWSER_VAULT_KEY      the saved logins' key (default ~/.config/everythingllm/browser-vault.key,
                         made on first use); the vaults are in <data>/vault/
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import logging
import os
import re
import secrets
import shutil
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import hostrpc
from chatimage import alt, link
from egress import config as egress_config
from hostrpc import RunnerError, safefs

from browser import page as pagetext
from browser.origin import host_of, normal_site, registrable, secure, site_matches
from browser.vault import Vault, VaultError, credential, totp

log = logging.getLogger("browser-runner")

IMAGE = "localhost/everythingllm-browser"  # hostctl browser-images builds it
LABEL = "everythingllm-browser=1"
PREFIX = "everythingllm-browser-"  # + the workspace: its container's name
PROFILE = "browser"  # egress.toml's profile, whose addresses the containers take
REPO = Path(__file__).resolve().parents[4]  # <repo>/packages/browser/src/browser/
CLIENT_PREFIX = "client-"  # the MCP gateway's clients' sandboxes, which have no browser
# As the sandbox's: workspace slugs and thread ids.
KEY_RE = re.compile(r"[a-z0-9_][a-z0-9_-]{0,99}")
SCREEN = "1280x800"
MEMORY = "2g"
IDLE = 20 * 60  # seconds unused and unwatched before a browser is stopped
GRANT = 10 * 60  # seconds the user's OK to use a login lasts
WAIT = 40  # what wait_approval waits at most, inside a skill call's patience
MAX_ANSWERS = 20  # the user's last answers kept, for a wait_approval that comes late
ASK_SECONDS = 30 * 60  # how long a request for a login waits for the user
KEEP_ASKED = 24 * 3600  # how long an answered one is kept, for its card to say so
MAX_ASKED = 10  # requests for logins waiting in a workspace at once
VAULT_KEY = Path("~/.config/everythingllm/browser-vault.key").expanduser()
START_SECONDS = 40  # for a container's driver to answer
# An op in the driver: its own limits are shorter (30 s for a page load).
DRIVER_SECONDS = 42
LIMIT = 8 << 20  # a driver's reply: a screenshot is a few hundred KB
LIVE_PORT = 8453
TAKEOVER_PORT = 8454
DOWNLOAD_BYTES = 256 << 20  # as browser.driver's: a bigger file isn't copied
ACTIVE = 30  # seconds after the agent's last op on a tab that it still reads as working
# The ops that are the agent at work in its chat's tab (not wait_approval, which waits).
WORK = {"open", "act", "read", "handoff", "close", "logins", "login", "code", "passkey",
        "ask_login"}  # fmt: skip

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


@dataclass
class Config:
    root: Path
    data: Path
    ips: dict[str, str]  # slot -> address on the network
    network: str
    proxy: str  # the egress proxy's public port, as Chromium's --proxy-server
    pages_url: str = ""  # where the live cards are (https :8445), "" for no cards
    takeover_url: str = f"http://127.0.0.1:{TAKEOVER_PORT}/"
    live_port: int = LIVE_PORT
    takeover_port: int = TAKEOVER_PORT
    repo: Path = REPO
    vault_key: Path = VAULT_KEY

    @classmethod
    def from_env(cls) -> Config:
        get = os.environ.get
        host = get("PUBLIC_HOST")
        egress = egress_config.load()
        return cls(
            root=Path(
                get("BROWSER_ROOT", hostrpc.data_dir() / "sandbox" / "workspaces")
            ),
            data=Path(get("BROWSER_DATA", hostrpc.data_dir() / "browser")),
            ips=dict(egress.profiles[PROFILE].ips),
            network=egress.network,
            proxy=egress.public_url,
            pages_url=f"https://{host}:8445/" if host else "",
            takeover_url=f"https://{host}:{TAKEOVER_PORT}/"
            if host
            else f"http://127.0.0.1:{TAKEOVER_PORT}/",
            live_port=int(get("BROWSER_LIVE_PORT", LIVE_PORT)),
            takeover_port=int(get("BROWSER_TAKEOVER_PORT", TAKEOVER_PORT)),
            vault_key=Path(get("BROWSER_VAULT_KEY", VAULT_KEY)),
        )

    def sockets(self, slot: str) -> Path:
        return self.data / "sockets" / slot

    def downloads(self, workspace: str) -> Path:
        return self.data / "downloads" / workspace


@dataclass(eq=False)
class Tab:
    """A thread's tab, as the runner knows it: for its card, which outlives the container."""

    id: str
    workspace: str
    thread: str
    title: str = ""
    url: str = ""
    last: str = "Opened the browser"  # what was done last, for the card
    open: bool = True
    shot: bytes = b""  # its latest screenshot (JPEG)
    shot_at: float = 0.0
    viewers: int = 0  # live cards streaming it
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def moved(self, last: str, view: dict[str, Any] | None = None) -> None:
        self.last = last
        if view:
            self.title, self.url = view.get("title") or "", view.get("url") or ""
        self.shot_at = 0.0  # the next frame takes a new one
        self.changed.set()


@dataclass(eq=False)
class Approval:
    """The agent waiting for the user's OK to use a saved login or passkey."""

    id: str
    login: str  # the login's id
    kind: str  # "login" or "passkey"
    site: str
    username: str
    thread: str  # the chat that asks: an OK is for it alone
    url: str  # its page's address, which the view shows
    answer: bool | None = None
    answered: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(eq=False)
class LoginRequest:
    """The agent asking the user for a login for the site of its chat's page."""

    id: str  # `lr-` and 32 hex digits: the card's and the form's only key
    workspace: str
    thread: str
    tab: str  # the tab's id
    sites: list[str]  # the site of the page and its parents, narrowest first
    url: str  # the page's address when it asked
    made: float
    state: str = "waiting"  # or "saving", "saved", "declined"
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def site(self) -> str:
        return self.sites[0]


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
    approval: Approval | None = None  # the OK the agent waits for
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


def as_who(username: str) -> str:
    return f" as {username}" if username else ""


class Gone(RunnerError):
    """The workspace's browser stopped under a call (its window was closed, or it crashed)."""


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


def check_scope(scope: Any) -> tuple[str, str]:
    """(workspace, thread) from a skill's scope, as the sandbox checks it."""
    if not isinstance(scope, dict):
        raise RunnerError("scope must be {workspace, thread}")
    workspace, thread = (
        str(scope.get("workspace") or ""),
        str(scope.get("thread") or ""),
    )
    for what, key in (("workspace", workspace), ("thread", thread)):
        if not KEY_RE.fullmatch(key):
            raise RunnerError(f"bad {what} '{key}'")
    if workspace.startswith(CLIENT_PREFIX) or scope.get("gateway"):
        raise RunnerError("the MCP gateway's clients have no browser")
    return workspace, thread


class Runner(hostrpc.Service):
    log = log

    def __init__(
        self,
        config: Config,
        podman: Podman = podman,
        now: Callable[[], float] = time.monotonic,
    ):
        super().__init__()
        self.config = config
        self.podman = podman
        self.now = now
        self.sessions: dict[str, Session] = {}  # workspace -> its running browser
        self.tabs: dict[str, Tab] = {}  # tab id -> tab
        # (workspace, thread) -> its tab, open or not: a thread keeps its tab, and its card,
        # from one container to the next
        self.threads: dict[tuple[str, str], Tab] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.asked: dict[str, LoginRequest] = {}  # request id -> the request
        # (workspace, thread) -> what to say of its downloads in its next read
        self.downloaded: dict[tuple[str, str], list[str]] = {}
        # (workspace, thread) -> the agent's ops running for it, and when its last ended
        self.busy: dict[tuple[str, str], int] = {}
        self.acted: dict[tuple[str, str], float] = {}
        self.vault = Vault(config.data / "vault", config.vault_key)

    async def reply(self, msg: dict[str, Any]) -> dict[str, Any]:
        """An op, noted as the agent at work in its chat's tab while it runs (`state`)."""
        try:
            key = check_scope((msg.get("args") or {}).get("scope"))
        except (RunnerError, AttributeError):
            key = None
        if key is None or msg.get("op") not in WORK:
            return await super().reply(msg)
        self.busy[key] = self.busy.get(key, 0) + 1
        self.at_work(key)
        try:
            return await super().reply(msg)
        finally:
            self.busy[key] -= 1
            if not self.busy[key]:
                del self.busy[key]
            self.acted[key] = self.now()
            self.at_work(key)

    def at_work(self, key: tuple[str, str]) -> None:
        if (tab := self.threads.get(key)) is not None:
            tab.changed.set()  # for its card to say so

    # --- containers ---

    def container_args(self, session: Session) -> list[str]:
        """The podman run for a workspace's browser: hardened like a service container, on
        egress-net at its slot's address with no DNS, its profile, downloads and sockets
        mounted (data only: noexec) and the repo read-only for the driver's code."""
        c, ws = self.config, session.workspace
        data = "rw,noexec,nosuid,nodev"
        home = c.root / ws  # only browser/, which no sandbox run mounts
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
            "-v", f"{home / 'browser' / 'profile'}:/profile:{data}",
            "-v", f"{c.downloads(ws)}:/downloads:{data}",
            "-v", f"{session.folder}:/run/browser:{data}",
            IMAGE,
        ]  # fmt: skip

    def free_slot(self) -> tuple[str, str] | None:
        taken = {s.slot for s in self.sessions.values()}
        return next(
            ((s, ip) for s, ip in self.config.ips.items() if s not in taken), None
        )

    async def session(self, workspace: str) -> Session:
        """The workspace's running browser, started if need be."""
        async with self.locks.setdefault(workspace, asyncio.Lock()):
            if (s := self.sessions.get(workspace)) is not None:
                s.used = self.now()
                return s
            return await self.start(workspace)

    async def start(self, workspace: str) -> Session:
        slot = self.free_slot()
        if slot is None:
            await self.evict()
            slot = self.free_slot()
        if slot is None:
            raise RunnerError(
                f"all {len(self.config.ips)} browsers are in use by other workspaces; try again in a while"
            )
        home = self.config.root / workspace
        for d in (home / "browser" / "profile", self.config.downloads(workspace)):
            d.mkdir(parents=True, exist_ok=True)
        folder = self.config.sockets(slot[0])
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.iterdir():  # a socket an earlier container left
            old.unlink(missing_ok=True)
        s = Session(workspace, *slot, secrets.token_urlsafe(24), folder, self.now())
        await self.podman(["rm", "-f", s.name], 30)
        code, _, err = await self.podman(self.container_args(s), 60)
        if code:
            raise RunnerError(f"the browser didn't start: {err.strip()[-500:]}")
        deadline = self.now() + START_SECONDS
        while True:
            try:
                await hostrpc.request(s.driver, "ping", {}, 5, name="browser")
                break
            except RunnerError:
                if self.now() > deadline:
                    _, out, err = await self.podman(
                        ["logs", "--tail", "20", s.name], 10
                    )
                    await self.podman(["rm", "-f", s.name], 30)
                    raise RunnerError(
                        f"the browser didn't come up in {START_SECONDS} s: {(err or out).strip()[-500:]}"
                    ) from None
                await asyncio.sleep(0.25)
        self.sessions[workspace] = s
        log.info("started %s's browser (%s, %s)", workspace, s.slot, s.ip)
        return s

    def watched(self, s: Session) -> bool:
        return s.viewers > 0 or any(
            t.viewers
            for t in self.tabs.values()
            if t.workspace == s.workspace and t.open
        )

    async def evict(self) -> None:
        """Stop the browser that's gone unused longest, if one isn't watched or the user's."""
        idle = [
            s
            for s in self.sessions.values()
            if not self.watched(s) and s.control == "agent"
        ]
        if idle:
            await self.stop(min(idle, key=lambda s: s.used).workspace)

    async def stop(self, workspace: str) -> None:
        s = self.sessions.pop(workspace, None)
        if s is None:
            return
        if s.approval is not None:  # its waiter hears it's stale
            s.approval.answered.set()
        if s.making:
            await self.save_made(s)
        await self.podman(["stop", "-t", "5", s.name], 30)
        await self.collect(workspace)
        self.forget(workspace)
        log.info("stopped %s's browser", workspace)

    def forget(self, workspace: str) -> None:
        """Mark the workspace's tabs closed: their cards show how they were left."""
        for tab in self.threads.values():
            if tab.workspace == workspace and tab.open:
                tab.open = False
                tab.moved(
                    "The browser closed; it opens again when the agent next browses here"
                )

    async def idle_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self.stop_idle()

    async def stop_idle(self) -> None:
        now = self.now()
        for s in list(self.sessions.values()):
            if self.watched(s):
                s.used = now
            elif now - s.used > IDLE:
                await self.stop(s.workspace)

    async def collect(self, workspace: str) -> None:
        """Copy the workspace's finished downloads to its /project/downloads."""
        told = await asyncio.to_thread(
            collect_downloads,
            self.config.downloads(workspace),
            self.config.root,
            workspace,
        )
        for thread, notes in told.items():
            self.downloaded.setdefault((workspace, thread), []).extend(notes)

    async def cleanup(self) -> None:
        """Remove what an earlier runner left: its containers (profiles stay)."""
        await self.podman(["rm", "-f", "--filter", f"label={LABEL}"], 120)

    # --- the driver ---

    async def call(self, s: Session, op: str, args: dict[str, Any]) -> Any:
        """An op in the workspace's driver. A browser that's gone (its window closed, a
        crash) is forgotten and raises Gone."""
        try:
            result = await hostrpc.request(
                s.driver, op, args, DRIVER_SECONDS, name="browser", limit=LIMIT
            )
        except RunnerError as e:
            if "isn't running" not in str(e) and "closed the connection" not in str(e):
                raise
        else:
            if thread := args.get("thread"):
                await self.collect(s.workspace)
                told = self.downloaded.pop((s.workspace, thread), [])
                if told and isinstance(result, dict) and "notes" in result:
                    result["notes"] = [*result["notes"], *told]
                elif told:  # for the thread's next read
                    self.downloaded[(s.workspace, thread)] = told
            return result
        if self.sessions.get(s.workspace) is s:
            del self.sessions[s.workspace]
            if s.approval is not None:  # its waiter hears it's stale
                s.approval.answered.set()
            self.forget(s.workspace)
            await self.podman(["rm", "-f", s.name], 30)
        raise Gone("the browser closed (its window was shut, or it crashed)")

    def agent_may_act(self, s: Session) -> None:
        if s.control == "user":
            why = f" ({s.reason})" if s.reason else ""
            raise RunnerError(
                f"the user has this workspace's browser{why}. When they say they're done, "
                "take it back with browser-handoff (done: true); until then, ask them in "
                "your reply."
            )

    def tab(self, workspace: str, thread: str) -> tuple[Tab, bool]:
        """The thread's tab, opened, and whether it's new to the chat (made or reopened)."""
        if (tab := self.threads.get((workspace, thread))) is not None:
            new, tab.open = not tab.open, True
            return tab, new
        tab = Tab(f"bw-{secrets.token_hex(8)}", workspace, thread)
        self.tabs[tab.id] = tab
        self.threads[(workspace, thread)] = tab
        return tab, True

    def card(self, tab: Tab) -> str:
        """The tab's live card, as runs.live.Live.card_line makes one; "" without a public URL."""
        if not self.config.pages_url:
            return ""
        page = f"{self.config.pages_url.rstrip('/')}/_live/browser/{tab.id}"
        subject = tab.title or tab.url or "a page"
        return f"[![{alt(f'Browser: {subject}')}]({link(page + '.jpg')})]({link(page)})"

    def takeover(self, s: Session, tab: Tab | None = None) -> str:
        url = f"{self.config.takeover_url.rstrip('/')}/{s.token}/"
        return url + (f"?tab={tab.id}" if tab else "")

    def login_card(self, req: LoginRequest) -> str:
        """A request's card line, as `card`; "" without a public URL."""
        if not self.config.pages_url:
            return ""
        page = f"{self.config.pages_url.rstrip('/')}/_live/browser/login/{req.id}"
        return f"[![{alt(f'Log in to {registrable(req.site)}')}]({link(page + '.png')})]({link(page)})"

    def login_form(self, req: LoginRequest) -> str:
        return f"{self.config.takeover_url.rstrip('/')}/login/{req.id}/"

    # --- ops ---

    async def op_open(self, scope: dict[str, Any], url: str) -> dict[str, Any]:
        workspace, thread = check_scope(scope)
        for attempt in (1, 2):
            s = await self.session(workspace)
            self.agent_may_act(s)
            try:
                view = await self.call(s, "open", {"thread": thread, "url": url})
                break
            except Gone:
                if attempt == 2:
                    raise
        tab, new = self.tab(workspace, thread)
        tab.moved(f"Opened {view.get('url') or url}", view)
        return {"page": pagetext.render(view), "card": self.card(tab), "new": new}

    async def running(self, workspace: str, thread: str) -> tuple[Session, Tab]:
        s = self.sessions.get(workspace)
        tab = self.threads.get((workspace, thread))
        if s is None or tab is None or not tab.open:
            raise RunnerError(
                "this chat has no page open in the browser; open one first"
            )
        s.used = self.now()
        return s, tab

    async def op_act(
        self, scope: dict[str, Any], action: str, ref: str = "", text: str = ""
    ) -> dict[str, Any]:
        workspace, thread = check_scope(scope)
        s, tab = await self.running(workspace, thread)
        self.agent_may_act(s)
        view = await self.call(
            s,
            "act",
            {"thread": thread, "action": action, "ref": ref or "", "text": text or ""},
        )
        tab.moved(describe(action, ref, text), view)
        return {"page": pagetext.render(view)}

    async def op_read(self, scope: dict[str, Any], find: str = "") -> dict[str, Any]:
        workspace, thread = check_scope(scope)
        s, tab = await self.running(workspace, thread)
        self.agent_may_act(s)  # nor watch what the user types
        view = await self.call(s, "read", {"thread": thread})
        tab.title, tab.url = view.get("title") or "", view.get("url") or ""
        return {"page": pagetext.render(view, (find or "").strip())}

    async def op_handoff(
        self, scope: dict[str, Any], reason: str = "", done: bool = False
    ) -> dict[str, Any]:
        """Give the user the browser, to do what `reason` says in the take-over view; or,
        `done`, take it back once they've said in the chat that they're finished."""
        workspace, thread = check_scope(scope)
        if done:
            if (s := self.sessions.get(workspace)) is None:
                raise RunnerError(
                    "this workspace's browser isn't open; open a page first"
                )
            await self.give_back(s)
            tab = self.threads.get((workspace, thread))
            if tab is None or not tab.open:
                return {"page": ""}
            view = await self.call(s, "read", {"thread": thread})
            tab.moved("The agent has the browser again", view)
            return {"page": pagetext.render(view)}
        s = await self.session(workspace)
        tab, _ = self.tab(workspace, thread)
        s.control, s.reason, s.asked = "user", (reason or "").strip()[:200], True
        await self.capture(s, True)
        tab.moved(f"Waiting for you: {s.reason}" if s.reason else "Waiting for you")
        if tab.url:
            await self.call(s, "front", {"thread": thread})
        return {"card": self.card(tab), "takeover": self.takeover(s, tab)}

    async def op_close(self, scope: dict[str, Any]) -> dict[str, Any]:
        workspace, thread = check_scope(scope)
        tab = self.threads.get((workspace, thread))
        if tab is None or not tab.open:
            return {}
        if (s := self.sessions.get(workspace)) is not None:
            self.agent_may_act(s)
        tab.open = False
        tab.moved("Closed this chat's tab")
        if (s := self.sessions.get(workspace)) is not None:
            await self.call(s, "close", {"thread": thread})
        return {}

    # --- saved logins ---

    async def op_logins(self, scope: dict[str, Any]) -> dict[str, Any]:
        """The workspace's saved logins (never a password or 2FA secret), and the site of
        the thread's page, whose logins are the ones that can fill there."""
        workspace, thread = check_scope(scope)
        logins = await asyncio.to_thread(self.vault.logins, workspace)
        tab = self.threads.get((workspace, thread))
        host = host_of(tab.url) if tab is not None and tab.open else ""
        for login in logins:
            login["here"] = bool(host) and site_matches(host, login["site"])
        return {"logins": logins, "site": host}

    async def op_login(
        self,
        scope: dict[str, Any],
        login: str,
        user_ref: str = "",
        pass_ref: str = "",
        submit: bool = False,
    ) -> dict[str, Any]:
        s, tab, entry = await self.usable(scope, login, "login")
        if waiting := self.approval(s, tab, entry):
            return waiting
        view = await self.call(
            s,
            "fill_login",
            {
                "thread": tab.thread,
                "site": entry["site"],
                "username": entry["username"],
                "password": entry.get("password", ""),
                "user_ref": user_ref or "",
                "pass_ref": pass_ref or "",
                "submit": bool(submit),
            },
        )
        await self.used(s.workspace, entry)
        tab.moved(
            f"{'Logged in' if submit else 'Filled in the login'} for {entry['site']}"
            + as_who(entry["username"]),
            view,
        )
        return {"page": pagetext.render(view)}

    async def op_code(
        self, scope: dict[str, Any], login: str, ref: str, submit: bool = False
    ) -> dict[str, Any]:
        s, tab, entry = await self.usable(scope, login, "login")
        if not entry.get("totp"):
            raise RunnerError(
                f"the {entry['site']} login has no 2FA secret saved; hand the browser to the user for the code"
            )
        if waiting := self.approval(s, tab, entry):
            return waiting
        view = await self.call(
            s,
            "fill_code",
            {"thread": tab.thread, "site": entry["site"], "code": totp(entry["totp"]),
             "ref": ref or "", "submit": bool(submit)},
        )  # fmt: skip
        await self.used(s.workspace, entry)
        tab.moved(f"Filled in the 2FA code for {entry['site']}", view)
        return {"page": pagetext.render(view)}

    async def op_passkey(
        self, scope: dict[str, Any], login: str, ref: str
    ) -> dict[str, Any]:
        s, tab, entry = await self.usable(scope, login, "passkey")
        if waiting := self.approval(s, tab, entry):
            return waiting
        view = await self.call(
            s,
            "sign_in_passkey",
            {"thread": tab.thread, "site": entry["rp_id"],
             "credential": credential(entry), "ref": ref or ""},
        )  # fmt: skip
        count = view.pop("sign_count", None)
        if count is None:
            tab.moved(f"The page didn't ask for the {entry['site']} passkey", view)
        else:
            await self.used(s.workspace, entry, sign_count=count)
            tab.moved(
                f"Signed in with a passkey for {entry['site']}"
                + as_who(entry["username"]),
                view,
            )
        return {"page": pagetext.render(view)}

    async def op_wait_approval(
        self, scope: dict[str, Any], approval: str
    ) -> dict[str, Any]:
        workspace, _ = check_scope(scope)
        s = self.sessions.get(workspace)
        # Unanswered and no longer asked (another login's request took its place, or the
        # browser restarted): not the user's no.
        stale = {"done": True, "approved": False, "stale": True}
        if s is None:
            return stale
        if approval in s.answers:
            return {"done": True, "approved": s.answers[approval]}
        waiting = s.approval
        if waiting is None or waiting.id != approval:
            return stale
        try:
            await asyncio.wait_for(waiting.answered.wait(), WAIT)
        except TimeoutError:
            return {"done": False, "approved": False}
        if waiting.answer is None:
            return stale
        return {"done": True, "approved": waiting.answer}

    async def op_ask_login(self, scope: dict[str, Any]) -> dict[str, Any]:
        """Ask the user for a login for the site of the thread's page, on a card: the site
        is the page's, never the model's. The thread's request for the same site, while
        it waits, is asked again rather than twice."""
        workspace, thread = check_scope(scope)
        s, tab = await self.running(workspace, thread)
        self.agent_may_act(s)
        host = host_of(tab.url)
        try:
            site = normal_site(host)
        except ValueError:
            raise RunnerError(
                f"this chat's page is on {host or 'no site'}, which can't have a saved login; "
                "hand the browser to the user with browser-handoff instead"
            ) from None
        self.prune_asked()
        req = next(
            (
                r
                for r in self.asked.values()
                if (r.workspace, r.thread, r.site) == (workspace, thread, site)
                and self.waiting(r)
            ),
            None,
        )
        if req is None:
            waiting = [
                r for r in self.asked.values()
                if r.workspace == workspace and self.waiting(r)
            ]  # fmt: skip
            if len(waiting) >= MAX_ASKED:
                raise RunnerError(
                    f"{MAX_ASKED} requests for logins are already waiting in this workspace; "
                    "ask the user to answer them first"
                )
            req = LoginRequest(
                f"lr-{secrets.token_hex(16)}", workspace, thread, tab.id,
                parent_sites(site), tab.url, self.now(),
            )  # fmt: skip
            self.asked[req.id] = req
        req.url = tab.url
        tab.moved(f"Waiting for your login for {site}")
        return {"request": req.id, "site": site, "card": self.login_card(req)}

    def waiting(self, req: LoginRequest) -> bool:
        return req.state == "waiting" and self.asked_left(req) > 0

    def asked_left(self, req: LoginRequest) -> float:
        """Seconds until a request runs out."""
        return req.made + ASK_SECONDS - self.now()

    def asked_state(self, req: LoginRequest) -> str:
        """waiting, saving, saved, declined, or expired (a wait that ran out)."""
        if req.state == "waiting" and not self.waiting(req):
            return "expired"
        return req.state

    def prune_asked(self) -> None:
        for req in [r for r in self.asked.values() if self.now() - r.made > KEEP_ASKED]:
            del self.asked[req.id]

    def asked_by_id(self, request: str) -> LoginRequest | None:
        """A request by its id (compared in constant time, as the view's token)."""
        self.prune_asked()
        return next(
            (r for r in self.asked.values() if secrets.compare_digest(r.id, request)),
            None,
        )

    async def fulfil(
        self,
        req: LoginRequest,
        site: str,
        username: str,
        password: str,
        totp: str,
        ask: bool,
    ) -> dict[str, Any]:
        """Save what the user sent for a request into the vault, for the site they chose
        of the request's own (the page's, or a parent of it), once: a second send while
        the first is being saved finds it no longer waiting."""
        if not self.waiting(req):
            raise RunnerError(
                f"this request isn't waiting any more ({self.asked_state(req)})"
            )
        if site not in req.sites:
            raise RunnerError(
                f"the login is for {' or '.join(req.sites)}, not '{site}'"
            )
        if not password:
            raise RunnerError("enter the password")
        req.state = "saving"
        try:
            saved = await asyncio.to_thread(
                self.vault.add, req.workspace, site, username, password, totp, ask
            )
        except BaseException:
            req.state = "waiting"  # for the user to fix and send again
            req.changed.set()
            raise
        self.answer_asked(req, "saved", f"You saved a login for {site}")
        return saved

    def decline(self, req: LoginRequest) -> None:
        if not self.waiting(req):
            raise RunnerError(
                f"this request isn't waiting any more ({self.asked_state(req)})"
            )
        self.answer_asked(req, "declined", f"You didn't give a login for {req.site}")

    def answer_asked(self, req: LoginRequest, state: str, last: str) -> None:
        req.state = state
        req.changed.set()
        tab = self.tabs.get(req.tab)
        if tab is not None and tab.open:
            tab.moved(last)

    async def usable(
        self, scope: dict[str, Any], login: str, kind: str
    ) -> tuple[Session, Tab, dict[str, Any]]:
        """The thread's browser and tab, and the saved entry of `kind` it names, if the
        agent may act and the tab's page is on the entry's site."""
        workspace, thread = check_scope(scope)
        s, tab = await self.running(workspace, thread)
        self.agent_may_act(s)
        entry = await asyncio.to_thread(
            self.vault.get, workspace, str(login or ""), kind
        )
        host = host_of(tab.url)
        if not site_matches(host, entry["site"]):
            raise RunnerError(
                f"that {kind} is for {entry['site']}, and this chat's page is on {host or 'no site'}; "
                f"open {entry['site']}'s sign-in page first"
            )
        if not secure(tab.url):
            raise RunnerError(
                f"a saved {kind} works only on an https page on its usual port; open "
                f"https://{host}/ instead"
            )
        return s, tab, entry

    def approval(
        self, s: Session, tab: Tab, entry: dict[str, Any]
    ) -> dict[str, str] | None:
        """The OK the agent must wait for before using `entry`, as the op's reply
        ({approval, card}), or None when it needs none (the entry doesn't ask, or the user
        said yes to this chat in the last GRANT seconds)."""
        key = (tab.thread, entry["id"])
        if not entry.get("ask") or s.granted.get(key, 0) > self.now():
            return None
        if s.approval is None or (s.approval.thread, s.approval.login) != key:
            if s.approval is not None:  # its waiter hears it's stale
                s.approval.answered.set()
            s.approval = Approval(
                secrets.token_hex(4), entry["id"], entry["kind"], entry["site"],
                entry["username"], tab.thread, tab.url,
            )  # fmt: skip
        tab.moved(f"Waiting for your OK to use your {entry['site']} {entry['kind']}")
        return {"approval": s.approval.id, "card": self.card(tab)}

    def answer(self, s: Session, approval: str, yes: bool) -> None:
        """The user's answer in the take-over view."""
        waiting = s.approval
        if waiting is None or waiting.id != approval:
            raise RunnerError("that request isn't waiting any more")
        waiting.answer = bool(yes)
        if yes:
            s.granted[(waiting.thread, waiting.login)] = self.now() + GRANT
        s.answers[waiting.id] = waiting.answer
        while len(s.answers) > MAX_ANSWERS:
            del s.answers[next(iter(s.answers))]
        s.approval = None
        waiting.answered.set()
        self.tell(
            s,
            f"You {'allowed' if yes else 'refused'} the {waiting.site} {waiting.kind}",
        )

    async def used(self, workspace: str, entry: dict[str, Any], **fields: Any) -> None:
        """Note the day a login was used (and a passkey's new sign count). The fill is done
        by then: a login deleted meanwhile, or a vault that can't be saved, doesn't make it
        a failure."""
        try:
            await asyncio.to_thread(
                self.vault.update,
                workspace,
                entry["id"],
                used=time.strftime("%Y-%m-%d"),
                **fields,
            )
        except VaultError as e:
            log.warning(
                "couldn't note a use of %s's login %s: %s", workspace, entry["id"], e
            )

    async def capture(self, s: Session, on: bool, user: bool = False) -> None:
        """Whether logins the user sends in the browser are offered for saving: while they
        have it. `user`: they took it in the take-over view, which unlocks a browser the
        agent sent part of a secret to (browser.driver)."""
        try:
            await self.call(s, "capture", {"on": on, "user": user})
        except RunnerError:
            pass  # a browser that's gone captures nothing

    async def offers(self, s: Session) -> list[dict[str, Any]]:
        try:
            return await self.call(s, "offers", {})
        except RunnerError:
            return []

    async def save_offer(
        self, s: Session, offer: str, username: str | None, ask: bool
    ) -> dict[str, Any]:
        """Save an offer, and only then drop it in the browser: one the vault refuses stays,
        for the user to fix and save again."""
        taken = await self.call(s, "peek_offer", {"id": offer})
        name = taken["username"] if username is None else str(username)
        saved = await asyncio.to_thread(
            self.vault.add, s.workspace, taken["site"], name, taken["password"], "", ask
        )
        try:
            await self.call(s, "drop_offer", {"id": offer})
        except RunnerError:
            pass  # saved; a browser that's gone has no offers left to show
        return saved

    async def make_passkeys(self, s: Session, on: bool) -> None:
        """Let the browser's pages make a passkey (`on`), while the user has it, or stop
        them; the driver stops them itself once one is made or time is up."""
        if on and s.control != "user":
            raise RunnerError("take over the browser first: only you make passkeys")
        await self.call(s, "make_passkeys", {"on": on})
        s.making = on
        if on:
            self.tell(s, "Waiting for a site to make a passkey")
        else:
            await self.save_made(s)

    async def save_made(self, s: Session) -> None:
        """Save the passkeys the browser's pages made, and note whether they still can."""
        try:
            got = await self.call(s, "made", {})
        except RunnerError:
            s.making = False  # a browser that's gone makes nothing more
            return
        s.making = got["making"]
        for one in got["made"]:
            try:
                saved = await asyncio.to_thread(
                    self.vault.add_passkey, s.workspace, one.get("credential")
                )
            except VaultError as e:
                log.warning("couldn't save a passkey %s made: %s", one.get("url"), e)
                s.made = f"The passkey a site made couldn't be saved: {e}"
            else:
                s.made = f"Saved the passkey you made for {saved['site']}" + as_who(
                    saved["username"]
                )
            self.tell(s, s.made)

    def tell(self, s: Session, last: str) -> None:
        """Say `last` on the cards of the workspace's open tabs."""
        for tab in self.tabs.values():
            if tab.workspace == s.workspace and tab.open:
                tab.moved(last)

    async def op_ping(self) -> dict[str, Any]:
        image, network = await asyncio.gather(
            self.podman(["image", "exists", IMAGE], 30),
            self.podman(["network", "exists", self.config.network], 30),
        )
        problems = []
        if image[0] != 0:
            problems.append(f"image {IMAGE} is missing (uv run hostctl browser-setup)")
        if network[0] != 0:
            problems.append(
                f"network {self.config.network} is missing (uv run hostctl egress-setup)"
            )
        if not (self.config.data / "novnc" / "core" / "rfb.js").is_file():
            problems.append(
                "noVNC isn't in place for the take-over view (uv run hostctl browser-setup)"
            )
        return {"problems": problems, "browsers": sorted(self.sessions)}

    # --- for the live card and the take-over view ---

    async def give_back(self, s: Session) -> None:
        """The agent has the browser again."""
        s.control, s.reason, s.asked = "agent", "", False
        await self.capture(s, False)  # which ends making passkeys
        if s.making:
            await self.save_made(s)
        self.tell(s, "The agent has the browser again")

    async def take(self, s: Session) -> None:
        """The user takes the browser from the take-over view."""
        if s.control != "user":
            s.control, s.reason, s.asked, s.made = "user", "you took over", False, ""
            await self.capture(s, True, user=True)
            self.tell(s, "You took over the browser")

    def by_token(self, token: str) -> Session | None:
        return next(
            (
                s
                for s in self.sessions.values()
                if secrets.compare_digest(s.token, token)
            ),
            None,
        )

    async def screenshot(self, tab: Tab, every: float) -> bytes:
        """The tab's screenshot, taken again when it's older than `every` seconds and its
        browser is up; the last one it had otherwise."""
        s = self.sessions.get(tab.workspace)
        if tab.open and s is not None and self.now() - tab.shot_at >= every:
            tab.shot_at = self.now()
            try:
                shot = await self.call(s, "screenshot", {"thread": tab.thread})
            except RunnerError:
                return tab.shot
            if shot.get("jpeg"):
                tab.shot = base64.b64decode(shot["jpeg"])
                tab.title, tab.url = (
                    shot.get("title") or tab.title,
                    shot.get("url") or tab.url,
                )
        return tab.shot

    def state(self, tab: Tab) -> str:
        """working, idle, waiting, user or closed: what's being done with the tab now (see
        the module's docstring)."""
        s = self.sessions.get(tab.workspace)
        if not tab.open or s is None:
            return "closed"
        if s.control == "user":
            return "waiting" if s.asked else "user"
        if (s.approval is not None and s.approval.thread == tab.thread) or any(
            r.tab == tab.id and self.waiting(r) for r in self.asked.values()
        ):
            return "waiting"
        key = (tab.workspace, tab.thread)
        if key in self.busy or (
            key in self.acted and self.now() - self.acted[key] < ACTIVE
        ):
            return "working"
        return "idle"

    def activity(self, s: Session) -> str:
        """The workspace's browser's state as the take-over view says it: the user's, or
        what's being done in the tab that's most to say about."""
        if s.control == "user":
            return "waiting" if s.asked else "user"
        states = {
            self.state(t)
            for t in self.tabs.values()
            if t.workspace == s.workspace and t.open
        }
        return next((w for w in ("waiting", "working") if w in states), "idle")


def parent_sites(site: str) -> list[str]:
    """`site` and the sites above it a login could be saved for, narrowest first:
    accounts.google.com, google.com; never a public suffix."""
    sites = [site]
    parts = site.split(".")
    for i in range(1, len(parts) - 1):
        try:
            sites.append(normal_site(".".join(parts[i:])))
        except ValueError:
            break
    return sites


def describe(action: str, ref: str, text: str) -> str:
    """An action as the card says it; what's typed isn't shown (it may be a password)."""
    what = {
        "click": "Clicked", "fill": "Filled in", "type": "Typed into", "press": "Pressed",
        "select": "Chose", "check": "Ticked", "uncheck": "Unticked", "hover": "Pointed at",
        "scroll_down": "Scrolled down", "scroll_up": "Scrolled up", "back": "Went back",
        "forward": "Went forward", "reload": "Reloaded", "wait": "Waited",
    }.get(action, action)  # fmt: skip
    if action == "press":
        return f"Pressed {text}"
    if action == "select":
        return f"Chose {text[:40]}"
    return f"{what} {ref}".strip()


async def serve(config: Config, stop: asyncio.Event | None = None) -> None:
    """Serve the runner on its socket, its live cards and the take-over view, until `stop`
    is set, or without one until SIGTERM; then stop every browser."""
    from browser import live, takeover

    runner = Runner(config)
    await runner.cleanup()
    loop = asyncio.get_running_loop()
    on_sigterm = stop is None
    if stop is None:
        stop = asyncio.Event()
        loop.add_signal_handler(signal.SIGTERM, stop.set)
    cards = await live.Live(runner).serve(config.live_port)
    view = await takeover.Takeover(runner).serve(config.takeover_port)
    idle = asyncio.create_task(runner.idle_loop())
    try:
        await hostrpc.serve(
            runner, hostrpc.socket_path("browser", "BROWSER_SOCKET"), stop=stop
        )
    finally:
        idle.cancel()
        cards.close()
        view.close()
        for workspace in list(runner.sessions):
            await runner.stop(workspace)
        if on_sigterm:
            loop.remove_signal_handler(signal.SIGTERM)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(serve(Config.from_env()))
