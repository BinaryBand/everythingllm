"""The agent's requests for a login (`ask_login`), as browser-runner keeps them: a card in
the chat links to a page of its own on the take-over view's origin (/login/<id>/,
browser.takeover) with a form for the site of the chat's page, and what the user sends
there goes into the vault. A request outlives the browser (a save needs only the vault)
until ASK_SECONDS pass, and is kept KEEP_ASKED after, for its card to say how it ended.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from typing import Any

from hostrpc import RunnerError

from browser.tabs import LoginRequest, Tab, parent_sites
from browser.vault import Vault

ASK_SECONDS = 30 * 60  # how long a request for a login waits for the user
KEEP_ASKED = 24 * 3600  # how long an answered one is kept, for its card to say so
MAX_ASKED = 10  # requests for logins waiting in a workspace at once


class LoginRequests:
    """The runner's requests for logins, by id. `vault` and `now` are asked each time, so
    they're the runner's as they are then."""

    def __init__(
        self,
        tabs: dict[str, Tab],
        vault: Callable[[], Vault],
        now: Callable[[], float],
    ):
        self.tabs = tabs  # the runner's: tab id -> tab
        self.vault = vault
        self.now = now
        self.asked: dict[str, LoginRequest] = {}  # request id -> the request

    def request(self, workspace: str, thread: str, tab: Tab, site: str) -> LoginRequest:
        """The thread's request for `site` while it waits, or a new one."""
        self.prune()
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
        return req

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

    def prune(self) -> None:
        for req in [r for r in self.asked.values() if self.now() - r.made > KEEP_ASKED]:
            del self.asked[req.id]

    def by_id(self, request: str) -> LoginRequest | None:
        """A request by its id (compared in constant time, as the view's token)."""
        self.prune()
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
                self.vault().add, req.workspace, site, username, password, totp, ask
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
