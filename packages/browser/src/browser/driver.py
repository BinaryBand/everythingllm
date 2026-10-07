"""browser.driver: drives one workspace's Chromium from inside its browser container
(host/containers/browser), for browser-runner on the host.

The container's entrypoint starts Xvfb and x11vnc (the take-over view's screen, on a Unix
socket), then this: Playwright launches Chromium on that screen with the workspace's
persistent profile, so its logins last from one container to the next, and everything it
fetches goes through the egress proxy's public port. browser-runner asks over
/run/browser/driver.sock (hostrpc); nothing here listens on the network.

Each chat thread has its own tab, made on its first `open`. A popup a tab opens (a login
window, a link with target=_blank) becomes the thread's tab until it closes. Downloads go
to /downloads/<thread>/, a folder of browser-runner's that no sandbox run can see (it copies
them to the workspace's /project/downloads, and says so in the thread's next read), at most
MAX_DOWNLOADS between two reads and DOWNLOAD_BYTES each; dialogs are answered on their own
(alerts accepted, confirms and prompts dismissed) and reported in the next read, as a
download that fails is.

Saved logins (browser.vault) come from the runner and go only into fields on their own
site: the page and the frame a field is in must be on the login's site (browser.origin), as
Playwright sees them, not the page's scripts; a password goes only into a password field;
and nothing filled is ever said back. A read hides any piece of a filled password or code
(PIECE characters of it) wherever it shows, and shows a field holding one as filled, even
once the page makes it a text field. A field holding one is the agent's only to submit,
leave or replace: typing, a key other than SECRET_KEYS, or a choice in it is refused, so
the agent can't make a value a read would show part of. And `press` sends only plain keys
(KEYS, and Shift with SHIFTED), never a shortcut, so nothing reaches the clipboard to be
pasted elsewhere. Passwords are hidden for the container's life (only stale 2FA codes are
let go), and so are those the user typed while they had the browser, sent or not. As a
read hides what the agent sends too, sending a guess and seeing it hidden would spell a
secret out: an address or text (or a run of key presses) holding a piece of one is
refused, and locks the browser to the agent until the user takes it over in the view. Chromium's own password saving is off. While the user has the browser, capture.js offers what they log in with for saving;
the runner asks the user in the take-over view, and saves the offer before dropping it.

Passkeys go through Chromium's WebAuthn virtual authenticator (over CDP, which nothing in
the page can reach): to sign in, one holding the saved passkey is put in the thread's page,
on the passkey's site, only while the button the agent names is clicked and the page asks
(at most PASSKEY_SECONDS), and Chromium itself checks that the page may use it. While the
user has the browser and asks to make one, every page has an empty authenticator until one
is made, MAKING_SECONDS pass or the hand-back, and a passkey a site makes in one is kept
for the runner to save (`made`). Nothing is typed, so there's nothing for a read to hide.

The ops that take a thread return the tab's view: {title, url, elements, text, more,
notes}, snapshot.js's reading of the page (browser.page renders it).

  open(thread, url)                 go to url in the thread's tab, made if need be
  act(thread, action, ref, text)    one of ACTIONS on the element `ref` from a read
                                    (press: a key of KEYS, or Shift with SHIFTED)
  read(thread)                      the view as it is
  fill_login(thread, site, username, password, user_ref, pass_ref, submit)
                                    a saved login into the fields with those refs
  fill_code(thread, site, code, ref, submit)  a 2FA code into the field `ref`
  screenshot(thread)                {jpeg (base64), title, url}; the front tab's without a thread
  front(thread)                     bring the thread's tab to the front of the window
  close(thread)                     close the thread's tab (and its popups)
  tabs()                            [{thread, title, url}]
  capture(on)                       whether logins the user sends are offered for saving
  offers()                          [{id, site, username}] of those, for OFFER_SECONDS
  peek_offer(id)                    {site, username, password}, kept until dropped
  drop_offer(id)                    forget it
  sign_in_passkey(thread, site, credential, ref)
                                    click `ref` with the passkey in the page -> the view
                                    and `sign_count` (None if the page never asked for it)
  make_passkeys(on)                 whether pages can make a passkey (the user's alone)
  made()                            {making, made: [{credential, url}]} since the last call

Config (environment):
  BROWSER_PROXY      where Chromium sends every request: the egress proxy's public port (required)
  BROWSER_RUN        the folder for driver.sock (default /run/browser)
  BROWSER_PROFILE    the persistent profile (default /profile)
  BROWSER_DOWNLOADS  where downloads are saved, in a folder per thread (default /downloads)
  BROWSER_SCREEN     the screen's size, as Xvfb has it (default 1280x800)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import signal
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus, unquote_plus

import hostrpc

from browser.origin import host_of, normal_site, secure, site_matches
from browser.page import MAX_ELEMENTS, REF_RE

log = logging.getLogger("browser-driver")

SNAPSHOT = (Path(__file__).with_name("snapshot.js")).read_text()
CAPTURE = (Path(__file__).with_name("capture.js")).read_text()
ACTIONS = (
    "click", "fill", "type", "press", "select", "check", "uncheck", "hover",
    "scroll_down", "scroll_up", "back", "forward", "reload", "wait",
)  # fmt: skip
NEEDS_REF = {"click", "fill", "type", "select", "check", "uncheck", "hover"}
NEEDS_TEXT = {"fill", "type", "press", "select"}
GOTO_MS = 30_000
ACT_MS = 10_000
SETTLE_MS = 3_000  # after an action, how long to let a page it started load
MAX_WAIT = 10  # seconds the `wait` action waits at most
SCROLL = 600  # pixels a scroll moves
JPEG_QUALITY = 65
SCHEMES = ("http://", "https://")
USERLIKE = {"text", "email", "tel", ""}  # input types a username goes into
CODELIKE = {"text", "tel", "number", "password", ""}  # and a 2FA code
MAX_OFFERS = 5
OFFER_SECONDS = 600  # how long a login the user sent waits to be saved
MAX_SECRET = 1000
PASSKEY_SECONDS = 15  # how long a sign-in waits for the page to ask for its passkey
MAX_MADE = 5  # passkeys made and not yet taken by the runner
MAKING_SECONDS = 5 * 60  # how long pages wait for a site to make a passkey
# A platform authenticator that answers with no prompt, and says the user was checked.
AUTHENTICATOR = {
    "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
    "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True,
}  # fmt: skip
MAX_DOWNLOADS = 10  # downloads a thread's pages may start between two reads
DOWNLOAD_BYTES = 256 << 20  # the largest download kept
MAX_CODES = 20  # the latest 2FA codes kept to hide (passwords are kept for the container's life)
PIECE = (
    6  # characters of a filled secret that a read never shows (all of a shorter one)
)
# The keys `press` sends, as Playwright names them, besides one printable character; and
# those it sends with Shift. No Control, Meta or Alt: no copying, cutting or pasting.
KEYS = {
    "Enter",
    "Tab",
    "Escape",
    "Backspace",
    "Delete",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "Home",
    "End",
    "PageUp",
    "PageDown",
    "Space",
}
SHIFTED = {
    "Tab",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "Home",
    "End",
    "PageUp",
    "PageDown",
}
# What the agent may do to a field holding a filled secret, besides click, fill and hover:
# the keys that submit it or leave it as it is.
SECRET_KEYS = {"Enter", "Tab", "Shift+Tab", "Escape"}
EDITS = {
    "type",
    "press",
    "select",
    "check",
    "uncheck",
}  # checked against filled secrets
FOCUSED = "input:focus, textarea:focus, [contenteditable]:focus"
TYPED = 64  # the single characters pressed last, checked as text
LOCKED = (
    "what you sent holds part of a saved login's secret, so this workspace's browser is "
    "locked to you until the user takes it over in the take-over view; tell them so in "
    "your reply"
)


def check_act(action: str, ref: str, text: str) -> None:
    """RunnerError unless `action` gets what it needs: a ref from a read, and some text."""
    if action not in ACTIONS:
        raise hostrpc.RunnerError(
            f"unknown action '{action}'; it's one of {', '.join(ACTIONS)}"
        )
    if action in NEEDS_REF and not REF_RE.fullmatch(ref or ""):
        raise hostrpc.RunnerError(
            f"{action} needs the ref of an element from the last read, like e12"
        )
    if ref and not REF_RE.fullmatch(ref):
        raise hostrpc.RunnerError(f"'{ref}' isn't a ref; they look like e12")
    if action in NEEDS_TEXT and not text:
        raise hostrpc.RunnerError(f"{action} needs text")
    if action == "press" and not key_allowed(text):
        raise hostrpc.RunnerError(
            f"'{text[:40]}' isn't a key press sends: one key (Enter, Tab, Escape, Backspace, "
            "an arrow, a character…) or Shift with Tab or an arrow; shortcuts with "
            "Control, Meta or Alt aren't sent"
        )


def key_allowed(key: str) -> bool:
    """Whether `press` sends `key`: one of KEYS, a printable character, or Shift+ one of
    SHIFTED, spelled as Playwright spells them. Anything else (a shortcut, an F key,
    Insert, a key code like KeyC) isn't guessed at."""
    if key in KEYS or (len(key) == 1 and key.isprintable()):
        return True
    modifier, _, rest = key.partition("+")
    return modifier == "Shift" and rest in SHIFTED


def check_url(url: str) -> str:
    """The address to open: http(s) only, and https:// added to a bare host."""
    url = (url or "").strip()
    if not url:
        raise hostrpc.RunnerError("open needs an address")
    if "://" not in url:
        url = "https://" + url
    if not url.lower().startswith(SCHEMES):
        raise hostrpc.RunnerError("only http and https addresses open here")
    return url


class Driver(hostrpc.Service):
    log = log

    def __init__(self, context: Any, downloads: Path):
        super().__init__()
        self.context = context
        self.downloads = downloads
        # thread -> its pages, the newest (a popup) last; and what to tell it next read
        self.stacks: dict[str, list[Any]] = {}
        self.notes: dict[str, list[str]] = {}
        self.downloading: dict[str, int] = {}  # thread -> downloads since its last read
        self.capturing = False
        # id -> {site, username, password, at}
        self.offers: dict[str, dict[str, Any]] = {}
        # Passwords filled from the vault or typed by the user, kept until the container
        # stops: dropping one would let a read show it again. 2FA codes go stale, so only
        # the last MAX_CODES are.
        self.passwords: list[str] = []
        self.codes: list[str] = []
        self.pieces: dict[int, set[str]] = {}  # theirs, as `pieces` makes them
        # Whether the agent sent part of a secret: it's refused until the user takes over.
        self.locked = False
        self.sites: dict[str, str] = {}  # a password -> the site it may be sent to
        self.guarding = False  # whether requests are routed through on_request
        self.typed: dict[str, str] = {}  # thread -> the last characters it pressed
        # While the user makes passkeys: each page's CDP session, and what was made.
        self.making = False
        self.deadline: asyncio.TimerHandle | None = None
        self.makers: dict[Any, Any] = {}
        self.made: list[dict[str, Any]] = []

    # --- tabs ---

    def owner(self, page: Any) -> str | None:
        return next((t for t, s in self.stacks.items() if page in s), None)

    def current(self, thread: str) -> Any | None:
        stack = self.stacks.get(thread, [])
        stack[:] = [p for p in stack if not p.is_closed()]
        return stack[-1] if stack else None

    def note(self, page: Any, text: str) -> None:
        if (thread := self.owner(page)) is not None:
            self.notes.setdefault(thread, []).append(text)

    def adopt(self, thread: str, page: Any) -> None:
        self.stacks.setdefault(thread, []).append(page)
        page.on("popup", lambda popup: self.on_popup(page, popup))
        page.on(
            "dialog", lambda dialog: asyncio.ensure_future(self.on_dialog(page, dialog))
        )
        page.on("download", lambda d: asyncio.ensure_future(self.on_download(page, d)))

    def on_popup(self, opener: Any, popup: Any) -> None:
        if (thread := self.owner(opener)) is not None:
            self.adopt(thread, popup)
            self.note(
                popup,
                "a new window opened; you're in it now (it closes back to the last one)",
            )

    async def on_dialog(self, page: Any, dialog: Any) -> None:
        kind, message = dialog.type, dialog.message[:300]
        try:
            if kind in ("alert", "beforeunload"):
                await dialog.accept()
            else:
                await dialog.dismiss()
        except Exception:  # noqa: BLE001, S110 - the page went away with its dialog
            pass
        answered = "accepted" if kind in ("alert", "beforeunload") else "dismissed"
        self.note(page, f"the page showed a {kind} ({answered}): {message}")

    async def on_download(self, page: Any, download: Any) -> None:
        """Save a download to /downloads/<thread>/, for the runner to copy to /project."""
        name = download_name(hide(download.suggested_filename, self.pieces))
        thread = self.owner(page)
        if thread is None:
            await cancel(download)
            return
        count = self.downloading.get(thread, 0) + 1
        self.downloading[thread] = count
        if count > MAX_DOWNLOADS:
            await cancel(download)
            if count == MAX_DOWNLOADS + 1:
                self.note(
                    page,
                    f"the page started more than {MAX_DOWNLOADS} downloads; the rest "
                    "were cancelled",
                )
            return
        folder = self.downloads / thread
        folder.mkdir(exist_ok=True)
        target = folder / name
        stem, suffix, n = target.stem, target.suffix, 1
        while target.exists() or target.is_symlink():
            n += 1
            target = folder / f"{stem}-{n}{suffix}"
        # Saved under a dot name the runner passes over, so it never copies half a file.
        part = folder / f".{target.name}.part"
        try:
            await download.save_as(part)
            if part.stat().st_size > DOWNLOAD_BYTES:
                self.note(
                    page,
                    f"{name} was over {DOWNLOAD_BYTES >> 20} MB, so it wasn't kept",
                )
            else:
                part.rename(target)
        except Exception as e:  # noqa: BLE001 - reported in the next read
            self.note(page, f"a download of {name} failed: {e}")
        finally:
            part.unlink(missing_ok=True)

    async def tab(self, thread: str) -> Any:
        """The thread's tab, made if it has none: the window's first blank tab if nobody has
        it yet, else a new one."""
        if (page := self.current(thread)) is not None:
            return page
        spare = next(
            (
                p
                for p in self.context.pages
                if self.owner(p) is None and p.url == "about:blank"
            ),
            None,
        )
        page = spare or await self.context.new_page()
        self.adopt(thread, page)
        return page

    def existing(self, thread: str) -> Any:
        if (page := self.current(thread)) is None:
            raise hostrpc.RunnerError("this chat has no page open; open one first")
        return page

    async def view(self, thread: str, page: Any) -> dict[str, Any]:
        try:
            snap = await page.evaluate(SNAPSHOT, MAX_ELEMENTS)
        except Exception:  # noqa: BLE001 - mid-navigation (an error page loading): once more
            await self.settle(page)
            snap = await self.snapshot(page)
        view = {
            **snap,
            "title": await title_of(page),
            "url": page.url,
            "notes": self.notes.pop(thread, []),
        }
        self.downloading.pop(thread, None)
        return scrub(view, self.pieces) if self.filled else view

    async def snapshot(self, page: Any) -> dict[str, Any]:
        try:
            return await page.evaluate(SNAPSHOT, MAX_ELEMENTS)
        except Exception as e:  # noqa: BLE001 - a page that won't run scripts
            return {
                "elements": [],
                "text": f"(couldn't read the page: {e})",
                "more": False,
            }

    async def settle(self, page: Any) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=SETTLE_MS)
        except Exception:  # noqa: BLE001, S110 - still loading: the read says what's there
            pass
        await asyncio.sleep(0.4)

    # --- ops ---

    async def op_open(self, thread: str, url: str) -> dict[str, Any]:
        url = check_url(url)
        self.check_sent(thread, url, unquote_plus(url))
        page = await self.tab(thread)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=GOTO_MS)
        except Exception as e:  # noqa: BLE001 - reported with what did load
            self.notes.setdefault(thread, []).append(
                f"loading {url} didn't finish: {first_line(e)}"
            )
        return await self.view(thread, page)

    async def op_act(
        self, thread: str, action: str, ref: str = "", text: str = ""
    ) -> dict[str, Any]:
        check_act(action, ref, text)
        if (
            action == "press" and len(text) == 1
        ):  # a run of single characters is text too
            self.typed[thread] = (self.typed.get(thread, "") + text)[-TYPED:]
            self.check_sent(thread, self.typed[thread])
        else:
            self.typed.pop(thread, None)
            self.check_sent(
                thread, text if action in ("fill", "type", "select") else ""
            )
        page = self.existing(thread)
        target = page.locator(f'[data-bw-ref="{ref}"]').first if ref else None
        try:
            await self.do(page, target, action, text, ref)
        except hostrpc.RunnerError:
            raise
        except Exception as e:  # noqa: BLE001 - Playwright's errors, for the agent
            raise hostrpc.RunnerError(f"{action} failed: {first_line(e)}") from None
        await self.settle(page)
        return await self.view(thread, self.existing(thread))

    async def do(
        self, page: Any, target: Any, action: str, text: str, ref: str = ""
    ) -> None:
        if target is not None and not await target.count():
            raise hostrpc.RunnerError(
                "that ref isn't on the page any more; read it again for current refs"
            )
        if self.filled and action in EDITS:
            if target is not None:
                holds, where = await self.secret_in(target), ref or "that field"
            else:
                holds, where = await self.focused_secret(page), "the focused field"
            if holds and not (action == "press" and text in SECRET_KEYS):
                raise hostrpc.RunnerError(
                    f"{where} holds a saved login's secret: it can only be submitted "
                    "(press Enter), left (Tab, Escape) or replaced (fill); log in again "
                    "with browser-login"
                )
        match action:
            case "click":
                await target.click(timeout=ACT_MS)
            case "fill":
                await target.fill(text, timeout=ACT_MS)
            case "type":
                await target.press_sequentially(text, delay=30, timeout=ACT_MS)
            case "press":
                if target is not None:
                    await target.press(text, timeout=ACT_MS)
                else:
                    await page.keyboard.press(text)
            case "select":
                try:
                    await target.select_option(label=text, timeout=ACT_MS)
                except Exception:  # noqa: BLE001 - no option by that label; try it as a value
                    await target.select_option(value=text, timeout=ACT_MS)
            case "check" | "uncheck":
                await target.set_checked(action == "check", timeout=ACT_MS)
            case "hover":
                await target.hover(timeout=ACT_MS)
            case "scroll_down" | "scroll_up":
                if target is not None:
                    await target.scroll_into_view_if_needed(timeout=ACT_MS)
                else:
                    await page.mouse.wheel(
                        0, SCROLL if action == "scroll_down" else -SCROLL
                    )
            case "back":
                await page.go_back(wait_until="domcontentloaded", timeout=GOTO_MS)
            case "forward":
                await page.go_forward(wait_until="domcontentloaded", timeout=GOTO_MS)
            case "reload":
                await page.reload(wait_until="domcontentloaded", timeout=GOTO_MS)
            case "wait":
                try:
                    seconds = float(text or 2)
                except ValueError:
                    seconds = 2
                await asyncio.sleep(min(MAX_WAIT, max(0, seconds)))

    async def op_read(self, thread: str) -> dict[str, Any]:
        self.check_sent(thread)
        return await self.view(thread, self.existing(thread))

    # --- saved logins ---

    async def on_site(
        self, page: Any, ref: str, site: str, what: str, does: str
    ) -> Any:
        """The element `ref` (`what`, for errors) as a locator, once the page is on `site`
        over https: where a saved login or passkey may go (`does`, for errors)."""
        if not REF_RE.fullmatch(ref or ""):
            raise hostrpc.RunnerError(
                f"{what} needs a ref from the last read, like e12"
            )
        host = host_of(page.url)
        if not site_matches(host, site):
            raise hostrpc.RunnerError(
                f"this chat's page is on {host or 'no site'}, not {site}: {does} only on "
                "its own site; read the page again"
            )
        if not secure(page.url):
            raise hostrpc.RunnerError(
                f"{does} only on an https page on its usual port; open the site's "
                "https:// page"
            )
        target = page.locator(f'[data-bw-ref="{ref}"]').first
        if not await target.count():
            raise hostrpc.RunnerError(
                f"{ref} isn't on the page any more; read it again"
            )
        return target

    async def field(self, page: Any, ref: str, site: str, kind: str) -> Any:
        """The input `ref` (its element handle, which is what gets filled, not whatever
        the ref finds later), once it's sure to be of `kind` on `site`. Where it is comes
        from Playwright (the page's address, the element's frame), and what it is from
        its selector and get_attribute, which run apart from the page's scripts: a page
        can rewrite what its own scripts see (`ownerDocument`, `getAttribute`), and the
        runner's idea of the page can be stale (a popup on the site, closed by the page
        that opened it, leaves that page behind)."""
        await self.on_site(page, ref, site, f"the {kind} field", "a saved login fills")
        kinds = {"password": {"password"}, "username": USERLIKE, "code": CODELIKE}[kind]
        target = page.locator(f'input[data-bw-ref="{ref}"]').first
        handle, kind_of = None, None
        try:
            if await target.count():
                handle = await target.element_handle(timeout=ACT_MS)
                frame = await handle.owner_frame()
                kind_of = ((await handle.get_attribute("type")) or "").lower()
        except Exception:  # noqa: BLE001 - gone mid-check
            raise hostrpc.RunnerError(
                f"{ref} couldn't be checked; read the page again"
            ) from None
        if handle is not None:
            frame_url = frame.url if frame is not None else ""
            where = host_of(frame_url)
            if not site_matches(where, site):
                raise hostrpc.RunnerError(
                    f"{ref} is on {where or 'no site'}, not {site}: a saved login fills "
                    "only on its own site"
                )
            if not secure(frame_url):
                raise hostrpc.RunnerError(
                    f"{ref} is in a frame that isn't https on its usual port, where a "
                    "saved login doesn't fill"
                )
        if kind_of not in kinds:
            raise hostrpc.RunnerError(
                f"{ref} isn't a {kind} field"
                + (
                    " (a password goes only into a password field)"
                    if kind == "password"
                    else ""
                )
            )
        return handle

    async def fill(
        self, page: Any, fields: list[tuple[Any, str]], submit: bool
    ) -> None:
        """Fill each (field, value) and press Enter in the last if `submit`. Errors say
        nothing of the value, nor pass on Playwright's text."""
        for target, value in fields:
            try:
                await target.fill(value, timeout=ACT_MS)
            except Exception:  # noqa: BLE001 - its text could hold the call
                raise hostrpc.RunnerError(
                    "a field couldn't be filled (hidden, read-only, or gone); read the page again"
                ) from None
        if submit and fields:
            try:
                await fields[-1][0].press("Enter", timeout=ACT_MS)
            except Exception:  # noqa: BLE001 - as above
                raise hostrpc.RunnerError(
                    "pressing Enter in the field failed"
                ) from None
        await self.settle(page)

    async def op_fill_login(
        self,
        thread: str,
        site: str,
        username: str = "",
        password: str = "",
        user_ref: str = "",
        pass_ref: str = "",
        submit: bool = False,
    ) -> dict[str, Any]:
        self.check_sent(thread)
        page = self.existing(thread)
        if not user_ref and not pass_ref:
            raise hostrpc.RunnerError(
                "give the ref of the username field, the password field, or both"
            )
        if pass_ref and not password:
            raise hostrpc.RunnerError("this saved login has no password")
        if user_ref and not username:
            raise hostrpc.RunnerError(
                "this saved login has no username; give only pass_ref"
            )
        fields = []  # every check before any filling
        if user_ref:
            fields.append(
                (await self.field(page, user_ref, site, "username"), username)
            )
        if pass_ref:
            fields.append(
                (await self.field(page, pass_ref, site, "password"), password)
            )
            self.keep_filled(password, site=site)
            await self.guard_requests()
        await self.fill(page, fields, submit)
        return await self.view(thread, self.existing(thread))

    async def op_fill_code(
        self, thread: str, site: str, code: str, ref: str, submit: bool = False
    ) -> dict[str, Any]:
        self.check_sent(thread)
        page = self.existing(thread)
        target = await self.field(page, ref, site, "code")
        self.keep_filled(code, code=True)
        await self.fill(page, [(target, code)], submit)
        return await self.view(thread, self.existing(thread))

    @property
    def filled(self) -> list[str]:
        return [*self.passwords, *self.codes]

    def keep_filled(self, secret: str, code: bool = False, site: str = "") -> None:
        """Remember a secret about to be filled (or that the user typed), so reads never
        say it back and the agent can't edit a field holding it; a password's `site`, so
        it's sent nowhere else (guard_requests)."""
        kept = self.codes if code else self.passwords
        if secret in kept:
            kept.remove(secret)
        kept.append(secret)
        del self.codes[:-MAX_CODES]
        self.pieces = pieces(self.filled)
        if site and not code:
            self.sites[secret] = site

    async def guard_requests(self) -> None:
        """Have every request the browser makes checked (`leak`) from the first password
        on: a form's action or a page's script could send one to another site, or in the
        clear, whatever field it was typed into."""
        if not self.guarding and self.context is not None:
            self.guarding = True
            await self.context.route("**/*", self.on_request)

    async def on_request(self, route: Any, request: Any) -> None:
        try:
            body = request.post_data_buffer or b""
        except Exception:  # noqa: BLE001 - a body that can't be read is sent as is
            body = b""
        if (site := leak(request.url, body, self.sites)) is None:
            await route.continue_()
            return
        await route.abort("blockedbyclient")
        log.warning("blocked a request carrying %s's password", site)
        try:
            page = request.frame.page
        except Exception:  # noqa: BLE001 - a service worker's: no page to tell
            return
        where = host_of(request.url) or "an address"
        clear = "" if secure(request.url) else " in the clear"
        self.note(
            page,
            f"the page tried to send your {site} password to {where}{clear}; it was blocked",
        )

    def check_sent(self, thread: str, *texts: str) -> None:
        """Refuse what the agent sends (an address, text to type) if it holds a piece of a
        filled secret, and lock the browser to it until the user takes over: a read hides
        such a piece, so sending guesses and seeing which come back hidden would spell a
        secret out. Single key presses are checked as the run they make."""
        if self.locked:
            raise hostrpc.RunnerError(LOCKED)
        if not self.filled:
            return
        if any(holds_secret(t, self.pieces) for t in texts if t):
            self.locked = True
            self.typed.clear()
            log.warning(
                "the agent sent part of a filled secret; locked until the user takes over"
            )
            raise hostrpc.RunnerError(LOCKED)

    async def secret_in(self, target: Any) -> bool:
        """Whether the element holds a piece of a filled secret, as Playwright reads its
        value (or, not being a field, its text), not the page's scripts. One that can't be
        read counts as holding one."""
        if not self.filled:
            return False
        try:
            try:
                value = await target.input_value(timeout=ACT_MS)
            except Exception:  # noqa: BLE001 - not an input, textarea or select
                value = await target.inner_text(timeout=ACT_MS)
        except Exception:  # noqa: BLE001 - gone, or unreadable: refuse rather than guess
            return True
        return holds_secret(value, self.pieces)

    async def focused_secret(self, page: Any) -> bool:
        """Whether the field a key pressed on the page goes to (the focused one, in any
        frame) holds a piece of a filled secret; a frame that can't be asked counts as
        one that does."""
        for frame in page.frames:
            try:
                focused = frame.locator(FOCUSED)
                n = await focused.count()
            except Exception:  # noqa: BLE001 - detached mid-check
                return True
            for i in range(n):
                if await self.secret_in(focused.nth(i)):
                    return True
        return False

    # --- passkeys ---

    async def authenticator(self, page: Any) -> tuple[Any, str]:
        """A CDP session on `page` with a virtual authenticator in it, and its id. `disarm`
        takes it out (WebAuthn.disable removes the page's authenticators)."""
        cdp = await self.context.new_cdp_session(page)
        try:
            await cdp.send("WebAuthn.enable", {"enableUI": False})
            added = await cdp.send(
                "WebAuthn.addVirtualAuthenticator", {"options": AUTHENTICATOR}
            )
        except Exception:
            await disarm(cdp)
            raise
        return cdp, added["authenticatorId"]

    async def op_sign_in_passkey(
        self, thread: str, site: str, credential: dict[str, Any], ref: str
    ) -> dict[str, Any]:
        self.check_sent(thread)
        page = self.existing(thread)
        target = await self.on_site(
            page,
            ref,
            site,
            "the button that signs in with a passkey",
            "a passkey signs in",
        )
        before = int(credential.get("signCount") or 0)
        try:
            cdp, authenticator = await self.authenticator(page)
        except Exception:  # noqa: BLE001 - the page went away
            raise hostrpc.RunnerError(
                "the page couldn't take a passkey; read it again"
            ) from None
        try:
            await cdp.send(
                "WebAuthn.addCredential",
                {"authenticatorId": authenticator,
                 "credential": {**credential, "rpId": site}},
            )  # fmt: skip
            try:
                await target.click(timeout=ACT_MS)
            except Exception as e:  # noqa: BLE001 - Playwright's errors, for the agent
                raise hostrpc.RunnerError(f"click failed: {first_line(e)}") from None
            count = await signed(cdp, authenticator, before)
        except hostrpc.RunnerError:
            raise
        except Exception:  # noqa: BLE001 - CDP's errors could hold the credential
            raise hostrpc.RunnerError("the passkey couldn't be used") from None
        finally:
            await disarm(cdp)
        await self.settle(page)
        if count is None:
            self.notes.setdefault(thread, []).append(
                f"the page didn't ask for the passkey within {PASSKEY_SECONDS} s of the "
                "click; find the button that signs in with a passkey, or hand the "
                "browser to the user"
            )
        return {**await self.view(thread, self.existing(thread)), "sign_count": count}

    async def op_make_passkeys(self, on: bool) -> dict[str, Any]:
        """Give every page (and each new one) an empty authenticator that keeps what a site
        makes in it (`on`), while the user has the browser, until a passkey is made or
        MAKING_SECONDS pass; or take them out, as the hand-back does."""
        if on and not self.capturing:
            raise hostrpc.RunnerError(
                "only the user makes passkeys, while they have it"
            )
        if on and not self.making:
            self.making = True
            self.deadline = asyncio.get_running_loop().call_later(
                MAKING_SECONDS, self.stop_making
            )
            self.context.on("page", self.on_page)
            for page in list(self.context.pages):
                await self.make_in(page)
        elif not on and self.making:
            self.making = False
            if self.deadline is not None:
                self.deadline.cancel()
            self.context.remove_listener("page", self.on_page)
            makers, self.makers = self.makers, {}
            for cdp in makers.values():
                await disarm(cdp)  # nothing, for a page that closed meanwhile
        return {}

    def stop_making(self) -> None:
        asyncio.ensure_future(self.op_make_passkeys(False))

    def on_page(self, page: Any) -> None:
        asyncio.ensure_future(self.make_in(page))

    async def make_in(self, page: Any) -> None:
        if page in self.makers:
            return
        try:
            cdp, _ = await self.authenticator(page)
        except Exception:  # noqa: BLE001 - closed before it could have one
            return
        if not self.making or page in self.makers:  # turned off, or armed, meanwhile
            await disarm(cdp)
            return
        self.makers[page] = cdp
        cdp.on("WebAuthn.credentialAdded", lambda event: self.on_made(page, event))

    def on_made(self, page: Any, event: Any) -> None:
        credential = event.get("credential") if isinstance(event, dict) else None
        if not self.making or not isinstance(credential, dict):
            return
        del self.made[: -(MAX_MADE - 1)]
        self.made.append({"credential": credential, "url": page.url})
        self.stop_making()  # one is what the user asked for

    async def op_made(self) -> dict[str, Any]:
        """The passkeys made since the last call (private keys and all, for the runner to
        save), and whether pages can still make one."""
        made, self.made = self.made, []
        return {"making": self.making, "made": made}

    # --- offering what the user logs in with ---

    def on_capture(self, source: dict[str, Any], data: Any) -> None:
        """capture.js's call (window.__bwCapture): kept only while the user has the browser,
        and for the site of the frame it came from, whatever the page says."""
        if not self.capturing or not isinstance(data, dict):
            return
        frame = source.get("frame")
        try:  # as the vault will save it: a frame on an address can't be offered
            site = normal_site(host_of(getattr(frame, "url", "") or ""))
        except ValueError:
            return
        username, password = data.get("username"), data.get("password")
        if not (
            site
            and isinstance(username, str)
            and isinstance(password, str)
            and password
        ):
            return
        if len(username) > MAX_SECRET or len(password) > MAX_SECRET:
            return
        for key in [
            k
            for k, o in self.offers.items()
            if (o["site"], o["username"]) == (site, username)
        ]:
            del self.offers[key]
        while len(self.offers) >= MAX_OFFERS:
            del self.offers[min(self.offers, key=lambda k: self.offers[k]["at"])]
        self.keep_filled(password, site=site)  # never read back, nor sent elsewhere
        self.offers[secrets.token_hex(4)] = {
            "site": site, "username": username, "password": password, "at": time.monotonic(),
        }  # fmt: skip

    def live_offers(self) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        for key in [k for k, o in self.offers.items() if now - o["at"] > OFFER_SECONDS]:
            del self.offers[key]
        return self.offers

    async def op_capture(self, on: bool, user: bool = False) -> dict[str, Any]:
        """Offer what the user logs in with while they have the browser (`on`); when it
        comes back, keep whatever is in a password field as a secret to hide, as they may
        have typed one without sending it. `user`: they took it in the take-over view
        themselves, which unlocks it."""
        if self.capturing and not on:
            await self.keep_typed()
            await self.op_make_passkeys(False)
        self.capturing = bool(on)
        if on and user:
            self.locked = False
        return {}

    async def keep_typed(self) -> None:
        for page in list(self.context.pages):
            for frame in page.frames:
                try:
                    fields = frame.locator("input[type=password]")
                    for i in range(await fields.count()):
                        value = await fields.nth(i).input_value(timeout=ACT_MS)
                        if value and len(value) <= MAX_SECRET:
                            self.keep_filled(value, site=site_of(frame.url))
                except Exception:  # noqa: BLE001, S112 - a frame gone mid-look
                    continue

    async def op_offers(self) -> list[dict[str, Any]]:
        return [
            {"id": k, "site": o["site"], "username": o["username"]}
            for k, o in self.live_offers().items()
        ]

    async def op_peek_offer(self, id: str) -> dict[str, Any]:
        offer = self.live_offers().get(id)
        if offer is None:
            raise hostrpc.RunnerError("that login isn't waiting to be saved any more")
        return {k: offer[k] for k in ("site", "username", "password")}

    async def op_drop_offer(self, id: str) -> dict[str, Any]:
        self.live_offers().pop(id, None)
        return {}

    async def op_screenshot(self, thread: str = "") -> dict[str, Any]:
        page = self.current(thread) if thread else self.front_page()
        if page is None:
            return {"jpeg": "", "title": "", "url": ""}
        shot = await page.screenshot(
            type="jpeg", quality=JPEG_QUALITY, timeout=5_000, animations="allow"
        )
        return {
            "jpeg": base64.b64encode(shot).decode(),
            "title": await title_of(page),
            "url": page.url,
        }

    def front_page(self) -> Any | None:
        pages = [p for p in self.context.pages if not p.is_closed()]
        return pages[-1] if pages else None

    async def op_front(self, thread: str) -> dict[str, Any]:
        if (page := self.current(thread)) is not None:
            await page.bring_to_front()
        return {}

    async def op_close(self, thread: str) -> dict[str, Any]:
        for page in self.stacks.pop(thread, []):
            if not page.is_closed():
                await page.close()
        self.notes.pop(thread, None)
        if not self.context.pages:  # closing the last tab would close the window
            await self.context.new_page()
        return {}

    async def op_tabs(self) -> list[dict[str, Any]]:
        tabs = []
        for thread in list(self.stacks):
            if (page := self.current(thread)) is not None:
                tabs.append(
                    {"thread": thread, "title": await title_of(page), "url": page.url}
                )
        return tabs


def site_of(url: str) -> str:
    """The site a login on a page at `url` would be saved for, or "" for none."""
    try:
        return normal_site(host_of(url))
    except ValueError:
        return ""


def leak(url: str, body: bytes, sites: dict[str, str]) -> str | None:
    """The site whose password a request to `url` with `body` carries somewhere other than
    to that site over https (in its address or body, as typed, URL-encoded or in JSON), or
    None. Only what can be seen: a page that encrypts or hashes one first isn't caught."""
    if not sites:
        return None
    host, safe = host_of(url), secure(url)
    raw = url.encode(errors="replace")
    for secret, site in sites.items():
        if len(secret) < PIECE or (safe and site_matches(host, site)):
            continue  # a short one would turn up in others' addresses by chance
        forms = {
            secret,
            quote_plus(secret),
            quote(secret, safe=""),
            json.dumps(secret)[1:-1],
        }
        if any(f.encode() in raw or f.encode() in body for f in forms):
            return site
    return None


def download_name(suggested: str) -> str:
    """A download's file name: the page's suggestion, cut to a plain name of its own."""
    name = Path(suggested.replace("\\", "/")).name
    name = "".join(c for c in name if c.isprintable()).strip().lstrip(".")[:120]
    return name or "download"


async def cancel(download: Any) -> None:
    try:
        await download.cancel()
    except Exception:  # noqa: BLE001, S110 - it finished or failed already
        pass


def pieces(filled: list[str]) -> dict[int, set[str]]:
    """Every PIECE characters of each secret (all of a shorter one), by length: as typed,
    and with its spaces squeezed as snapshot.js shows a value."""
    out: dict[int, set[str]] = {}
    for secret in filled:
        for form in {secret, re.sub(r"\s+", " ", secret).strip()}:
            if form:
                k = min(PIECE, len(form))
                out.setdefault(k, set()).update(
                    form[i : i + k] for i in range(len(form) - k + 1)
                )
    return out


def hide(text: str, pieces: dict[int, set[str]]) -> str:
    """`text` with every run of it made of pieces of a filled secret replaced by •••: what
    is left beside a run is less than a piece, so a secret shows whole, cut short, or with
    something added, and none of it is read."""
    if not text or not pieces:
        return text
    covered = [False] * len(text)
    for k, found in pieces.items():
        for i in range(len(text) - k + 1):
            if text[i : i + k] in found:
                covered[i : i + k] = [True] * k
    if not any(covered):
        return text
    out, i = [], 0
    while i < len(text):
        if covered[i]:
            while i < len(text) and covered[i]:
                i += 1
            out.append("•••")
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def holds_secret(text: str, pieces: dict[int, set[str]]) -> bool:
    return hide(text, pieces) != text


def scrub(view: dict[str, Any], pieces: dict[int, set[str]]) -> dict[str, Any]:
    """A read with no piece of a filled secret in it. snapshot.js never says a password
    field's value, but a page can make the field a text one (a "show password" button, which
    the agent can click), a 2FA code often goes into a text field, and a page can show
    either in its text. A field holding one is shown as filled. The address keeps its host,
    which the runner checks logins against."""
    url = view.get("url") or ""
    at = url.find("/", url.find("://") + 3) if "://" in url else 0
    if at >= 0:
        url = url[:at] + hide(url[at:], pieces)
    return {
        **view,
        "elements": [
            hide(line, pieces).replace(' value="•••"', " (filled)")
            for line in view.get("elements") or []
        ],
        "text": hide(view.get("text") or "", pieces),
        "title": hide(view.get("title") or "", pieces),
        "url": url,
        "notes": [hide(note, pieces) for note in view.get("notes") or []],
    }


async def title_of(page: Any) -> str:
    try:
        return await page.title()
    except Exception:  # noqa: BLE001 - mid-navigation; the address says where it is
        return ""


def first_line(e: Exception) -> str:
    return (str(e).strip().splitlines() or [type(e).__name__])[0][:300]


def chromium_args(proxy: str, screen: tuple[int, int]) -> list[str]:
    """Chromium's flags: every request through the proxy (the container has no other way
    out, nor DNS), sized to the screen, and not slowed down in a background tab, since the
    agent and the live card both use tabs the window isn't showing. A browser that ended
    badly (out of memory, a forced stop) doesn't offer to restore its pages: nobody asked
    for them, and the bubble sat over every take-over view."""
    width, height = screen
    return [
        f"--proxy-server={proxy}",
        "--proxy-bypass-list=<-loopback>",
        "--disk-cache-size=104857600",
        "--window-position=0,0",
        f"--window-size={width},{height}",
        "--start-maximized",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-crash-restore-bubble",
        "--password-store=basic",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=Translate,MediaRouter,OptimizationHints",
    ]


def quiet_password_manager(profile: Path) -> None:
    """Turn Chromium's own password saving off in the profile, so what the user types in
    the take-over view isn't kept there: the vault is where logins go."""
    prefs = profile / "Default" / "Preferences"
    try:
        data = json.loads(prefs.read_text())
    except (OSError, ValueError):
        data = {}
    data["credentials_enable_service"] = False
    data.setdefault("profile", {})["password_manager_enabled"] = False
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text(json.dumps(data))


async def signed(cdp: Any, authenticator: str, before: int) -> int | None:
    """The passkey's sign count once the page has used it (it goes up with each use), or
    None if it hasn't in PASSKEY_SECONDS."""
    until = time.monotonic() + PASSKEY_SECONDS
    while time.monotonic() < until:
        found = await cdp.send(
            "WebAuthn.getCredentials", {"authenticatorId": authenticator}
        )
        counts = [c.get("signCount", 0) for c in found.get("credentials", [])]
        if counts and counts[0] > before:
            return counts[0]
        await asyncio.sleep(0.25)
    return None


async def disarm(cdp: Any) -> None:
    """Take the page's authenticators out and leave it to Chromium's own WebAuthn."""
    try:
        await cdp.send("WebAuthn.disable")
        await cdp.detach()
    except Exception:  # noqa: BLE001, S110 - the page is gone, and its authenticator with it
        pass


def screen_size(value: str) -> tuple[int, int]:
    width, _, height = value.partition("x")
    return int(width), int(height)


async def serve() -> None:
    from playwright.async_api import (  # ty: ignore[unresolved-import] - the image has it
        async_playwright,
    )

    get = os.environ.get
    proxy = get("BROWSER_PROXY") or ""
    if not proxy:
        raise SystemExit("BROWSER_PROXY isn't set")
    run = Path(get("BROWSER_RUN", "/run/browser"))
    downloads = Path(get("BROWSER_DOWNLOADS", "/downloads"))
    screen = screen_size(get("BROWSER_SCREEN", "1280x800"))
    profile = Path(get("BROWSER_PROFILE", "/profile"))
    quiet_password_manager(profile)
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            str(profile),
            headless=False,
            no_viewport=True,
            accept_downloads=True,
            args=chromium_args(proxy, screen),
        )
        driver = Driver(context, downloads)
        await context.expose_binding("__bwCapture", driver.on_capture)
        await context.add_init_script(script=CAPTURE)
        stop = asyncio.Event()
        # The window was closed: end the container.
        context.on("close", lambda _: stop.set())
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, stop.set)
        try:
            await hostrpc.serve(
                driver,
                run / "driver.sock",
                limit=8 << 20,
                stop=stop,
            )
        finally:
            try:
                await context.close()
            except Exception:  # noqa: BLE001, S110 - already closed with its window
                pass


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(serve())


if __name__ == "__main__":
    main()
