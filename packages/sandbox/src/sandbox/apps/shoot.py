"""An app's page as a picture, for its card (sandbox.appshot): run by the sandbox runner in
a container of the browser image with no network (containers.prepare_shot).

  python3 /sandbox/shoot.py <page url> <workspace> <theme> <time zone>

/public is the workspace's /public, read-only. Chromium opens the page at its own address on
the pages site, but nothing leaves the container: a request on that site under the
workspace's prefix is answered from /public, the page's write-back and sync (/_apps/) are
held unanswered, so the page shows no failure and saves nothing, and anything else is
refused. The page is told the card's theme as its colour scheme and the user's time zone, gets SETTLE seconds once it
has loaded and its fonts are in, and its top VIEW (CSS pixels, at SCALE) is saved as
/out/shot.png. Standard library and Playwright only: it runs outside the repo's venv.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

VIEW = (480, 360)
SCALE = 3
SETTLE = 0.6
LOAD_TIMEOUT = 15_000  # ms
PUBLIC = Path("/public")
OUT = Path("/out/shot.png")
MAX_FILE = 16 << 20


def origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def held(url: str, site: str) -> bool:
    """Whether `url` is the page's write-back or sync, which is never answered."""
    return origin(url) == site and urlsplit(url).path.startswith("/_apps/")


def local(url: str, site: str, workspace: str, public: Path = PUBLIC) -> Path | None:
    """The file in `public` that `url` names on the pages site `site`, under the
    workspace's prefix (a folder's being its index.html), or None when it names none."""
    if origin(url) != site:
        return None
    path = unquote(urlsplit(url).path)
    prefix = f"/{workspace}/"
    if not path.startswith(prefix) or "\0" in path:
        return None
    rest = path[len(prefix) :]
    if rest == "" or rest.endswith("/"):
        rest += "index.html"
    root = public.resolve()
    target = (root / rest).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target


async def shoot(url: str, workspace: str, theme: str, zone: str) -> None:
    from playwright.async_api import (  # ty: ignore[unresolved-import] - the image has it
        async_playwright,
    )

    site = origin(url)
    waiting = []  # the held requests, kept so nothing answers them

    async def answer(route) -> None:
        asked = route.request.url
        if held(asked, site):
            waiting.append(route)
            return
        file = local(asked, site, workspace)
        if file is None or file.stat().st_size > MAX_FILE:
            await route.abort()
            return
        kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        await route.fulfill(status=200, body=file.read_bytes(), content_type=kind)

    os.makedirs(os.environ.get("HOME", "/tmp/home"), exist_ok=True)
    async with async_playwright() as pw:
        # The image has full Chromium, not the headless shell: its new headless mode.
        browser = await pw.chromium.launch(channel="chromium", args=["--no-first-run"])
        context = await browser.new_context(
            viewport={"width": VIEW[0], "height": VIEW[1]},
            device_scale_factor=SCALE,
            color_scheme="light" if theme == "light" else "dark",
            timezone_id=zone,
            service_workers="block",
            accept_downloads=False,
        )
        await context.route("**/*", answer)
        page = await context.new_page()
        await page.goto(url, wait_until="load", timeout=LOAD_TIMEOUT)
        await page.wait_for_function(
            "document.fonts.status === 'loaded'", timeout=LOAD_TIMEOUT
        )
        await asyncio.sleep(SETTLE)
        await page.screenshot(path=str(OUT), animations="disabled", caret="hide")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(shoot(*sys.argv[1:5]))
