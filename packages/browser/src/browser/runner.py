"""browser-runner: one Chromium per AnythingLLM workspace, for the browse skills
(anythingllm/agent-skills/browse, browser-act, browser-read, browser-handoff,
browser-login), with a live card in the chat and a take-over view on its own HTTPS port.

A workspace's browser is a podman container (host/containers/browser) started on its first
call and stopped when nobody has used or watched it for IDLE seconds. Its profile (cookies,
logins, history) is the workspace's alone and outlives the container:

  <data>/profiles/<workspace>/          the profile, which no sandbox run can see
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
(`handoff` with `done`), or when the browser is stopped. Only while they have it do the
view's VNC keys and its text field (type_text, press_key) reach the browser; what they type
there goes to the driver and is never logged, said back or shown on a card.

What the card and the take-over view say of a tab (`state`): `working` while one of the
agent's ops for its chat runs and for ACTIVE seconds after (it thinks between steps),
`idle` once the agent holds it and does nothing with it, `waiting` while the agent waits
for the user (it handed the browser over, or waits for their OK or a login in that chat),
`user` when the user took it, and `closed`.

Saved logins (browser.vault, one vault per workspace) are the agent's to use and never to
read: it names a login and the fields, and the runner has the driver fill them on the
login's own site, over https. A login the user marked `ask` waits for their OK in the
take-over view (and on the card) before each use, good for GRANT minutes in that chat
alone. A chat waits for one OK at a time: its new request takes the place of its own last
one, never another chat's, and one unanswered for APPROVAL_SECONDS is let go. While the
user has the browser, logins they send are offered for saving there too.

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
outlives the browser (a save needs only the vault) until ASK_SECONDS pass (browser.logins).

Ops (each takes `scope`):
  open(url)                      go to url in the thread's tab -> {page, card, new}
  act(action, ref?, text?)       one browser.driver action -> {page}
  read(find?)                    the page as it is, or its lines with `find` -> {page}
  handoff(reason)                give the user the browser -> {card, takeover, reason}:
                                 `reason` as the user sees it, naming the identity
                                 provider when the page is its sign-in (tabs.SIGN_IN)
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
  label(ref)                     the name the thread's last view gave element `ref`, for
                                 the skill's progress line -> {label}, "" if it gave none

`page` is browser.page's text; `card` the tab's live card line (browser.live), "" without
PUBLIC_HOST; `new` whether the card is new to this chat (the tab was just made).

Config (environment):
  BROWSER_SOCKET  the socket to listen on (default <storage>/everythingllm/browser/runner.sock);
                  the rest is browser.config's
"""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import signal
import time
from collections.abc import Callable
from typing import Any

import hostenv
import hostrpc
from chatimage import linked_image
from hostrpc import RunnerError

from browser import page as pagetext
from browser.config import Config, check_scope
from browser.containers import (
    IMAGE,
    LABEL,
    Gone,
    Podman,
    Session,
    collect_downloads,
    container_args,
    podman,
)
from browser.logins import LoginRequests
from browser.origin import host_of, normal_site, registrable, secure, site_matches
from browser.tabs import (
    Approval,
    LoginRequest,
    Tab,
    as_who,
    describe,
    sign_in_provider,
    site_of,
)
from browser.vault import Vault, VaultError, credential, totp

log = logging.getLogger("browser-runner")

IDLE = 20 * 60  # seconds unused and unwatched before a browser is stopped
GRANT = 10 * 60  # seconds the user's OK to use a login lasts
WAIT = 40  # what wait_approval waits at most, inside a skill call's patience
APPROVAL_SECONDS = 30 * 60  # an unanswered request for the user's OK is let go after
MAX_ANSWERS = 20  # the user's last answers kept, for a wait_approval that comes late
START_SECONDS = 40  # for a container's driver to answer
# An op in the driver: its own limits are shorter (30 s for a page load).
DRIVER_SECONDS = 42
LIMIT = 8 << 20  # a driver's reply: a screenshot is a few hundred KB
USER_KEYS = {"Enter", "Tab", "Backspace"}  # as browser.driver's: the view's key buttons
ACTIVE = 30  # seconds after the agent's last op on a tab that it still reads as working
POLLED = (
    10  # seconds after a client last asked for a tab's card that it still watches it
)
# The ops that are the agent at work in its chat's tab (not wait_approval, which waits).
WORK = {"open", "act", "read", "handoff", "close", "logins", "login", "code", "passkey",
        "ask_login"}  # fmt: skip


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
        # Slots a browser is starting or stopping in: not free, though no session has them.
        self.reserved: set[str] = set()
        self.tabs: dict[str, Tab] = {}  # tab id -> tab
        # (workspace, thread) -> its tab, open or not: a thread keeps its tab, and its card,
        # from one container to the next
        self.threads: dict[tuple[str, str], Tab] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        # (workspace, thread) -> what to say of its downloads in its next read
        self.downloaded: dict[tuple[str, str], list[str]] = {}
        # (workspace, thread) -> the agent's ops running for it, and when its last ended
        self.busy: dict[tuple[str, str], int] = {}
        self.acted: dict[tuple[str, str], float] = {}
        self.vault = Vault(config.data / "vault", config.vault_key)
        # The requests for logins: they read the runner's vault and clock as they are.
        self.login_requests = LoginRequests(
            self.tabs, lambda: self.vault, lambda: self.now()
        )
        self.asked = self.login_requests.asked  # request id -> the request

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
            now = self.now()
            # Past ACTIVE an op says nothing more (`state`): let go of every chat's so old.
            self.acted = {k: t for k, t in self.acted.items() if now - t < ACTIVE}
            self.acted[key] = now
            self.at_work(key)

    def at_work(self, key: tuple[str, str]) -> None:
        if (tab := self.threads.get(key)) is not None:
            tab.changed.set()  # for its card to say so

    # --- containers ---

    def container_args(self, session: Session) -> list[str]:
        return container_args(self.config, session)

    def free_slot(self) -> tuple[str, str] | None:
        taken = {s.slot for s in self.sessions.values()} | self.reserved
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
        # Another workspace's start mustn't take this slot (and wipe its sockets) before
        # this one has a session in it.
        self.reserved.add(slot[0])
        try:
            for d in (self.config.profile(workspace), self.config.downloads(workspace)):
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
        finally:
            self.reserved.discard(slot[0])

    def watched(self, s: Session) -> bool:
        """A live card streams one of its tabs, a client asks for one's card (browser.chats)
        or the take-over view is open."""
        return s.viewers > 0 or any(
            t.viewers or self.now() - t.polled_at < POLLED
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
        # Held until its container, which holds the address, is gone.
        self.reserved.add(s.slot)
        try:
            self.unask(s, *s.approvals.values())
            if s.making:
                await self.save_made(s)
            await self.podman(["stop", "-t", "5", s.name], 30)
        finally:
            self.reserved.discard(s.slot)
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
            try:  # one failure mustn't leave every idle browser running from then on
                await self.stop_idle()
            except Exception:
                log.exception("stopping idle browsers failed")

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
        except hostrpc.Unreachable:
            pass  # gone: forgotten below
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
            self.unask(s, *s.approvals.values())
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
        return linked_image(f"Browser: {self.subject(tab)}", page + ".jpg", page)

    def subject(self, tab: Tab) -> str:
        """The tab's page as its card names it: by its title, or by its site when it has
        none or a bot check's, never by its address, whose path can hold a token."""
        if tab.title and not pagetext.challenge_title(tab.title):
            return tab.title
        return site_of(tab.url)

    def takeover(self, s: Session, tab: Tab | None = None) -> str:
        url = f"{self.config.takeover_url.rstrip('/')}/{s.token}/"
        return url + (f"?tab={tab.id}" if tab else "")

    def login_card(self, req: LoginRequest) -> str:
        """A request's card line, as `card`; "" without a public URL."""
        if not self.config.pages_url:
            return ""
        page = f"{self.config.pages_url.rstrip('/')}/_live/browser/login/{req.id}"
        return linked_image(f"Log in to {registrable(req.site)}", page + ".png", page)

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
        label = tab.labels.get(ref or "", "")
        view = await self.call(
            s,
            "act",
            {"thread": thread, "action": action, "ref": ref or "", "text": text or ""},
        )
        tab.moved(describe(action, label or ref, str(text or "")), view)
        return {"page": pagetext.render(view)}

    async def op_label(self, scope: dict[str, Any], ref: str = "") -> dict[str, Any]:
        """What the thread's last view called element `ref` (the page's own words, cut
        short), for the line the skill shows in the chat while it acts on it."""
        workspace, thread = check_scope(scope)
        tab = self.threads.get((workspace, thread))
        if tab is None or not tab.open:
            return {"label": ""}
        return {"label": tab.labels.get(str(ref or "").strip(), "")}

    async def op_read(
        self, scope: dict[str, Any], find: str = "", card: bool = False
    ) -> dict[str, Any]:
        """The chat's page as text. With `card`, its card too, for a user who asks to see the
        browser: given whenever the chat has a tab, even one closed, stopped or the user's,
        with the reason the page can't be read in its place."""
        workspace, thread = check_scope(scope)
        if card is True:
            if (tab := self.threads.get((workspace, thread))) is None:
                raise RunnerError(
                    "this chat hasn't used the browser yet, so it has no card; open a "
                    "page first"
                )
            try:
                page = (await self.op_read(scope, find))["page"]
            except RunnerError as e:
                page = f"The page can't be read now: {e}"
            return {"page": page, "card": self.card(tab)}
        s, tab = await self.running(workspace, thread)
        self.agent_may_act(s)  # nor watch what the user types
        view = await self.call(s, "read", {"thread": thread})
        tab.seen(view)
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
        if tab.url:
            front = await self.call(s, "front", {"thread": thread})
            # The page as it is now (a sign-in popup, say), not as the agent last read it.
            where = (front or {}).get("url") or tab.url
            if provider := sign_in_provider(where):
                said = f" ({s.reason})" if s.reason else ""
                line = f"Sign in to {provider}, then hand the browser back{said}"
                s.reason = line[:200]
        tab.moved(f"Waiting for you: {s.reason}" if s.reason else "Waiting for you")
        return {
            "card": self.card(tab),
            "takeover": self.takeover(s, tab),
            "reason": s.reason,
        }

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
        # Unanswered and no longer asked (its chat asked for another login, nobody answered
        # in APPROVAL_SECONDS, or the browser restarted): not the user's no.
        stale = {"done": True, "approved": False, "stale": True}
        if s is None:
            return stale
        if approval in s.answers:
            return {"done": True, "approved": s.answers[approval]}
        waiting = s.approvals.get(approval)
        if waiting is None:
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
        req = self.login_requests.request(workspace, thread, tab, site)
        req.url = tab.url
        tab.moved(f"Waiting for your login for {site}")
        return {"request": req.id, "site": site, "card": self.login_card(req)}

    # The requests for logins (browser.logins), for live, chats and takeover.

    def waiting(self, req: LoginRequest) -> bool:
        return self.login_requests.waiting(req)

    def asked_left(self, req: LoginRequest) -> float:
        return self.login_requests.asked_left(req)

    def asked_state(self, req: LoginRequest) -> str:
        return self.login_requests.asked_state(req)

    def prune_asked(self) -> None:
        self.login_requests.prune()

    def asked_by_id(self, request: str) -> LoginRequest | None:
        return self.login_requests.by_id(request)

    async def fulfil(
        self,
        req: LoginRequest,
        site: str,
        username: str,
        password: str,
        totp: str,
        ask: bool,
    ) -> dict[str, Any]:
        return await self.login_requests.fulfil(
            req, site, username, password, totp, ask
        )

    def decline(self, req: LoginRequest) -> None:
        self.login_requests.decline(req)

    def answer_asked(self, req: LoginRequest, state: str, last: str) -> None:
        self.login_requests.answer_asked(req, state, last)

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
        self.expire(s)
        now = self.now()
        key = (tab.thread, entry["id"])
        if not entry.get("ask") or s.granted.get(key, 0) > now:
            return None
        # One per chat: its new request takes the place of its own last one, and leaves
        # other chats' be. A workspace's main chat and its scheduled jobs share the thread
        # "default" (anythingllm/agent-skills/_lib/scope.js), so theirs replace each other.
        waiting = next(
            (a for a in s.approvals.values() if a.thread == tab.thread), None
        )
        if waiting is None or waiting.login != entry["id"]:
            if waiting is not None:
                self.unask(s, waiting)
            waiting = Approval(
                secrets.token_hex(4), entry["id"], entry["kind"], entry["site"],
                entry["username"], tab.thread, tab.url, now,
            )  # fmt: skip
            s.approvals[waiting.id] = waiting
        tab.moved(f"Waiting for your OK to use your {entry['site']} {entry['kind']}")
        return {"approval": waiting.id, "card": self.card(tab)}

    def expire(self, s: Session) -> None:
        """Let go of the requests nobody answered in APPROVAL_SECONDS, saying so on their
        chats' cards, and of the OKs past their time."""
        now = self.now()
        old = [a for a in s.approvals.values() if now - a.made > APPROVAL_SECONDS]
        self.unask(s, *old)
        for waiting in old:
            tab = self.threads.get((s.workspace, waiting.thread))
            if tab is not None and tab.open:
                tab.moved(
                    f"Nobody answered in time about the {waiting.site} {waiting.kind}"
                )
        s.granted = {k: until for k, until in s.granted.items() if until > now}

    def unask(self, s: Session, *approvals: Approval) -> None:
        """Let unanswered requests go: their waiters hear they're stale."""
        for waiting in approvals:
            s.approvals.pop(waiting.id, None)
            waiting.answered.set()

    def answer(self, s: Session, approval: str, yes: bool) -> None:
        """The user's answer in the take-over view, said on the card of the chat that
        asked alone: another chat still waiting keeps saying so."""
        waiting = s.approvals.pop(approval, None)
        if waiting is None:
            raise RunnerError("that request isn't waiting any more")
        waiting.answer = bool(yes)
        if yes:
            s.granted[(waiting.thread, waiting.login)] = self.now() + GRANT
        s.answers[waiting.id] = waiting.answer
        while len(s.answers) > MAX_ANSWERS:
            del s.answers[next(iter(s.answers))]
        waiting.answered.set()
        tab = self.threads.get((s.workspace, waiting.thread))
        if tab is not None and tab.open:
            tab.moved(
                f"You {'allowed' if yes else 'refused'} the {waiting.site} {waiting.kind}"
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
        if on:
            s.offering = True
        try:
            await self.call(s, "capture", {"on": on, "user": user})
        except RunnerError:
            pass  # a browser that's gone captures nothing

    async def offers(self, s: Session) -> list[dict[str, Any]]:
        """The logins the user sent that the driver offers for saving. Only while they
        have the browser can it get new ones, so once the agent has it back and the driver
        has none left (saved, dropped or run out), it isn't asked again until then."""
        if s.control != "user" and not s.offering:
            return []
        try:
            offers = await self.call(s, "offers", {})
        except RunnerError:
            return []
        if not offers and s.control != "user":
            s.offering = False
        return offers

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

    async def type_text(
        self, s: Session, text: str, secret: bool = False, tab: str = ""
    ) -> None:
        """The user's text from the take-over view's field into the focused field of the
        page the view shows (the tab `tab`, or the one last brought to the front), as text,
        only while they have the browser; `secret`: they sent it as a password. It goes to
        the driver and nowhere else: no reply, log line or card holds it."""
        thread = self.typing_thread(s, tab)
        if not isinstance(text, str) or not text:
            raise RunnerError("there's nothing to type")
        await self.call(
            s, "user_type", {"text": text, "secret": secret is True, "thread": thread}
        )

    async def press_key(self, s: Session, key: str, tab: str = "") -> None:
        """One of USER_KEYS, from the take-over view's buttons, as type_text."""
        thread = self.typing_thread(s, tab)
        if key not in USER_KEYS:
            raise RunnerError(f"the key is one of {', '.join(sorted(USER_KEYS))}")
        await self.call(s, "user_key", {"key": key, "thread": thread})

    def typing_thread(self, s: Session, tab: str) -> str:
        """The thread of the tab the view shows, "" for none (or one closed since), once
        the user has the browser, as VNC's keys reach it only then (the view makes them
        view-only)."""
        if s.control != "user":
            raise RunnerError("take over the browser first: the agent has it")
        s.used = self.now()
        found = self.tabs.get(tab) if tab else None
        if found is None or found.workspace != s.workspace or not found.open:
            return ""
        return found.thread

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
        if any(a.thread == tab.thread for a in s.approvals.values()) or any(
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


async def serve(config: Config, stop: asyncio.Event | None = None) -> None:
    """Serve the runner on its socket, its live cards and the take-over view, until `stop`
    is set, or without one until SIGTERM; then stop every browser."""
    from browser import live, takeover

    config.root.mkdir(parents=True, exist_ok=True)  # the sandbox's, for downloads
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
            runner, hostenv.socket_path("browser", "BROWSER_SOCKET"), stop=stop
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
