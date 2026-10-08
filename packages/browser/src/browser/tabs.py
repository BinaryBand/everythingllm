"""What browser-runner keeps of a chat's browsing for its card and the take-over view: a
thread's tab (Tab), the agent waiting for the user's OK to use a saved login (Approval) or
for a login it asked for (LoginRequest), and how the card names a page and says what the
agent did on it (site_of, labels, describe).
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

from browser import page as pagetext
from browser.origin import host_of, is_public_suffix, normal_site, registrable


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
    # ref -> its element's name in the last view the agent got, to say what it acts on
    labels: dict[str, str] = field(default_factory=dict)

    def seen(self, view: dict[str, Any]) -> None:
        """Note what a view the agent got says of the page."""
        self.title, self.url = view.get("title") or "", view.get("url") or ""
        self.labels = labels(view)

    def moved(self, last: str, view: dict[str, Any] | None = None) -> None:
        self.last = last
        if view:
            self.seen(view)
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


def as_who(username: str) -> str:
    return f" as {username}" if username else ""


def site_of(url: str) -> str:
    """Who a page belongs to, for a card with no better name: the registrable name
    (origin.registrable) or the host, never the address, whose path can hold a token."""
    host = host_of(url).removeprefix("www.")
    if not host:
        return "a page"
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        return host
    if "." not in host or is_public_suffix(host):
        return host
    return registrable(host)


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


# An element as page.render lists it: [e12] button "Sign in" -> …, (checked), value="…"
ELEMENT_RE = re.compile(
    r'\[(e\d{1,6})\] \S+ "(.*?)"(?=$| -> | \(| value="| placeholder="| options: )'
)
LABEL_CHARS = 60


def labels(view: dict[str, Any]) -> dict[str, str]:
    """ref -> the element's name, from a view's elements, without the colon a form's label
    ends in ("Telephone:"); none for an unnamed one."""
    found = {}
    for line in view.get("elements") or []:
        if (m := ELEMENT_RE.match(str(line))) and (
            name := " ".join(m[2].split()).rstrip(":").rstrip()
        ):
            found[m[1]] = pagetext.clip(name, LABEL_CHARS)
    return found


def describe(action: str, ref: str, text: str) -> str:
    """An action as the card says it, `ref` being the element's name when it has one; what's
    typed isn't shown (it may be a password)."""
    what = {
        "click": "Clicked", "fill": "Filled in", "type": "Typed into", "press": "Pressed",
        "select": "Chose", "check": "Ticked", "uncheck": "Unticked", "hover": "Pointed at",
        "scroll_down": "Scrolled down", "scroll_up": "Scrolled up", "back": "Went back",
        "forward": "Went forward", "reload": "Reloaded", "wait": "Waited",
    }.get(action, action)  # fmt: skip
    if action == "press":  # a named key only: single keys could spell a password
        named = len(text.removeprefix("Shift+")) > 1
        return f"Pressed {text}" if named else "Pressed a key"
    if action == "select":
        return f"Chose {text[:40]}"
    return f"{what} {ref}".strip()
