"""Pictures of apps' pages for their live cards (sandbox.appsweb), in sandbox-runner.

An app's card shows its page as it renders now: apps/shoot.py opens the page the runner
rendered (/public/apps/<name>/index.html) in Chromium, in a container of the browser image
with no network that sees only the workspace's /public, read-only (containers.prepare_shot).
The page is the agent's code, so the picture is read back without following a symlink, at
most MAX_SHOT bytes, and drawn into the card (apps.card.pictured) only once Pillow finds a
PNG of a usable size in it.

A picture is kept by app, theme and a hash of the rendered page, which holds the app's data
and version, so an unchanged app is never taken again; a newer page's replaces it, and the
last KEEP apps and themes are kept. One is taken at a time on the machine, in a sandbox slot
like a build's, and a picture asked for while it's being taken waits for that one. One that
fails (no browser image here, a page that doesn't load in SHOT.max_timeout) is kept as None,
so the card stays drawn from the data until the page changes.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import shutil
from collections import OrderedDict
from typing import TYPE_CHECKING

import hostenv
from hostrpc import safefs

from sandbox.apps.card import pictured
from sandbox.containers import SHOT, prepare_shot
from sandbox.workspace import Scope

if TYPE_CHECKING:
    from sandbox.runner import Runner

log = logging.getLogger("sandbox.appshot")

KEEP = 32
MAX_SHOT = 8 << 20
# What's hashed of a rendered page: the runner keeps them far smaller.
MAX_PAGE = 16 << 20

Key = tuple[str, str, str, str]  # workspace, app, theme, the rendered page's hash


class Shots:
    def __init__(self, runner: Runner):
        self.runner = runner
        self.kept: OrderedDict[Key, bytes | None] = OrderedDict()
        self.going: dict[Key, asyncio.Task[bytes | None]] = {}
        self.turn = asyncio.Lock()

    def key(self, s: Scope, name: str, theme: str) -> Key | None:
        """The picture's key: None while the app has no rendered page."""
        page = safefs.read_regular(s.public, ("apps", name, "index.html"), MAX_PAGE)
        if page is None:
            return None
        return (s.workspace, name, theme, hashlib.sha256(page).hexdigest())

    def kept_card(self, key: Key | None) -> bytes | None:
        """The app's card with the picture kept under `key`, or None."""
        if key not in self.kept:
            return None
        self.kept.move_to_end(key)
        return self.kept[key]

    async def take(
        self, key: Key | None, s: Scope, name: str, title: str
    ) -> bytes | None:
        """The app's card with a picture of its page as it is now (`key`), taken unless it's
        kept; None when it can't be. The picture is taken in a task of its own, so a viewer
        who leaves meanwhile doesn't stop it for the others."""
        if key is None:
            return None
        if key in self.kept:
            return self.kept_card(key)
        if key not in self.going:
            self.going[key] = asyncio.create_task(self.keep(key, s, name, title))
        return await asyncio.shield(self.going[key])

    async def keep(self, key: Key, s: Scope, name: str, title: str) -> bytes | None:
        """Take the picture and keep it under `key` in place of an older page's, or None
        when it can't be taken."""
        try:
            card = await self.shoot(s, name, title, key[2])
        # A card is a nicety: one whose picture can't be taken is drawn instead.
        except Exception:
            log.exception("couldn't take a picture of %s/%s", s.workspace, name)
            card = None
        finally:
            del self.going[key]
        for older in [k for k in self.kept if k[:3] == key[:3]]:
            del self.kept[older]
        self.kept[key] = card
        while len(self.kept) > KEEP:
            self.kept.popitem(last=False)
        return card

    async def shoot(self, s: Scope, name: str, title: str, theme: str) -> bytes | None:
        runner = self.runner
        container = f"sandbox-shot-{secrets.token_hex(6)}"
        run_dir = runner.config.scripts / container
        url = runner.public_url(s.workspace, f"apps/{name}/")
        try:
            args = await asyncio.to_thread(prepare_shot, s, container, run_dir)
            async with self.turn, runner.slot():
                code, out, err, timed_out = await runner.podman(
                    [*args, url, s.workspace, theme, hostenv.user_zone().key],
                    SHOT.max_timeout,
                    container,
                )
            if timed_out or code != 0:
                why = f"took over {SHOT.max_timeout} s" if timed_out else (err or out)
                log.warning(
                    "no picture of %s/%s: %s", s.workspace, name, why.strip()[-500:]
                )
                return None
            shot = await asyncio.to_thread(
                safefs.read_regular, run_dir / "out", ("shot.png",), MAX_SHOT + 1
            )
        finally:
            await runner.podman(["rm", "-f", "--ignore", container], 60, None)
            await asyncio.to_thread(shutil.rmtree, run_dir, True)
        if shot is None or len(shot) > MAX_SHOT:
            log.warning("no picture of %s/%s: none saved", s.workspace, name)
            return None
        try:
            return await asyncio.to_thread(pictured, title, shot, theme)
        except ValueError as e:
            log.warning("no picture of %s/%s: %s", s.workspace, name, e)
            return None
