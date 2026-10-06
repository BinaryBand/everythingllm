"""browser.driver: drives one workspace's Chromium from inside its browser container
(host/containers/browser), for browser-runner on the host.

The container's entrypoint starts Xvfb and x11vnc (the take-over view's screen, on a Unix
socket), then this: Playwright launches Chromium on that screen with the workspace's
persistent profile, so its logins last from one container to the next, and everything it
fetches goes through the egress proxy's public port. browser-runner asks over
/run/browser/driver.sock (hostrpc); nothing here listens on the network.

Each chat thread has its own tab, made on its first `open`. A popup a tab opens (a login
window, a link with target=_blank) becomes the thread's tab until it closes. Downloads go
to /downloads (the workspace's /project/downloads in the sandbox); dialogs are answered
on their own (alerts accepted, confirms and prompts dismissed) and reported in the next
read, as downloads are.

Every op takes the thread and returns the tab's view: {title, url, elements, text, more,
notes}, snapshot.js's reading of the page (browser.page renders it).

  open(thread, url)                 go to url in the thread's tab, made if need be
  act(thread, action, ref, text)    one of ACTIONS on the element `ref` from a read
  read(thread)                      the view as it is
  screenshot(thread)                {jpeg (base64), title, url}; the front tab's without a thread
  front(thread)                     bring the thread's tab to the front of the window
  close(thread)                     close the thread's tab (and its popups)
  tabs()                            [{thread, title, url}]

Config (environment):
  BROWSER_PROXY      where Chromium sends every request: the egress proxy's public port (required)
  BROWSER_RUN        the folder for driver.sock (default /run/browser)
  BROWSER_PROFILE    the persistent profile (default /profile)
  BROWSER_DOWNLOADS  where downloads are saved (default /downloads)
  BROWSER_SCREEN     the screen's size, as Xvfb has it (default 1280x800)
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import signal
from pathlib import Path
from typing import Any

import hostrpc

from browser.page import MAX_ELEMENTS, REF_RE

log = logging.getLogger("browser-driver")

SNAPSHOT = (Path(__file__).with_name("snapshot.js")).read_text()
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
        name = Path(download.suggested_filename).name or "download"
        target = self.downloads / name
        stem, suffix, n = target.stem, target.suffix, 1
        while target.exists():
            n += 1
            target = self.downloads / f"{stem}-{n}{suffix}"
        try:
            await download.save_as(target)
            self.note(
                page, f"downloaded {target.name} to /project/downloads/{target.name}"
            )
        except Exception as e:  # noqa: BLE001 - reported in the next read
            self.note(page, f"a download of {name} failed: {e}")

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
        return {
            **snap,
            "title": await title_of(page),
            "url": page.url,
            "notes": self.notes.pop(thread, []),
        }

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
        page = self.existing(thread)
        target = page.locator(f'[data-bw-ref="{ref}"]').first if ref else None
        try:
            await self.do(page, target, action, text)
        except hostrpc.RunnerError:
            raise
        except Exception as e:  # noqa: BLE001 - Playwright's errors, for the agent
            raise hostrpc.RunnerError(f"{action} failed: {first_line(e)}") from None
        await self.settle(page)
        return await self.view(thread, self.existing(thread))

    async def do(self, page: Any, target: Any, action: str, text: str) -> None:
        if target is not None and not await target.count():
            raise hostrpc.RunnerError(
                "that ref isn't on the page any more; read it again for current refs"
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
        return await self.view(thread, self.existing(thread))

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
    agent and the live card both use tabs the window isn't showing."""
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
        "--password-store=basic",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=Translate,MediaRouter,OptimizationHints",
    ]


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
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            get("BROWSER_PROFILE", "/profile"),
            headless=False,
            no_viewport=True,
            accept_downloads=True,
            args=chromium_args(proxy, screen),
        )
        stop = asyncio.Event()
        context.on(
            "close", lambda _: stop.set()
        )  # the window was closed: end the container
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, stop.set)
        try:
            await hostrpc.serve(
                Driver(context, downloads),
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
