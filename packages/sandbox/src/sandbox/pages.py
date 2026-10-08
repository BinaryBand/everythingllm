"""A sandbox workspace's pages and the images it shows in the chat.

A workspace's /public is its pages, served as it is on the workspace pages site (:8447).
What the runner says about a page there: its address, what the site's CSP blocks in it and
its notices (what the sandbox the site runs its scripts in takes away, and that it has
scripts), and its link card. A file, folder or site build is copied into /public whole and
swapped in (`put_public`). show_image's images go on the pages site (:8445), under IMAGES.

A page's title for its card, and what its CSP blocks and its notices, are read without
following a symlink: a run could leave one in /public pointing at another workspace's
files."""

from __future__ import annotations

import hashlib
import html as htmllib
import io
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import chatimage
import chatimage.card
from hostrpc import safefs
from PIL import Image

from sandbox.errors import SandboxError
from sandbox.workspace import (
    Config,
    Scope,
    copy_regular,
    regular_files,
    remove_path,
    resolve,
    trim_images,
)

WRITE_BYTES = 1_000_000  # op_write's most, and the most of a page's file checked
PUBLISH_MAX_BYTES = 500 << 20  # what publish or a build copies into /public at once
CSP_SCAN = 200  # HTML and .js files of a changed page checked (blocked, notices)
LIST_MAX = 200  # pages listed, and files named in a run's changed list
# show_image: the images it puts on the pages site for the chat (`IMAGES/<workspace>/`), by
# the format Pillow reads in the file's header, at most IMAGE_MAX_BYTES each; past
# IMAGES_MAX_BYTES a workspace's oldest go, and old chats show them broken.
IMAGES = "_images"
IMAGE_FORMATS = {"PNG": "png", "JPEG": "jpg", "GIF": "gif", "WEBP": "webp"}
IMAGE_MAX_BYTES = 10 << 20
IMAGES_MAX_BYTES = 500 << 20

# What the pages site's CSP blocks, so a page that leans on it renders without it.
# Links to other sites are fine; loading from them isn't. Tags are matched with [^<>]*, so
# a page full of stray "<" can't make a pattern scan to the end of it again and again.
_OFFSITE = r"""["']?\s*(?:https?:)?//"""
_CSP_BLOCKED = (
    (
        "scripts from another host",
        re.compile(r"<script\b[^<>]*\bsrc\s*=" + _OFFSITE, re.IGNORECASE),
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


def csp_blocked(html: str, origin: str = "") -> list[str]:
    """What in a page the site's CSP will block: [] when nothing is. URLs on the site's own
    `origin` (scheme://host:port, as zola writes them) are allowed."""
    if origin:  # only where the origin ends: https://site.example.evil/ is another host
        html = re.sub(re.escape(origin) + r"""(?=[/"'\s>)]|$)""", "", html)
    return [what for what, pattern in _CSP_BLOCKED if pattern.search(html)]


# What the agent should know about a page whose scripts run: the pages site runs them in a
# CSP sandbox (allow-scripts allow-downloads), where each page has an opaque origin of its
# own. tests/test_pages_browser.py checks each of these limits in a real browser.
SCRIPTS_RE = re.compile(
    r"""<script\b|<[^<>]*\son[a-z]+\s*=|(?:href|src)\s*=\s*["']?\s*javascript:""",
    re.IGNORECASE,
)
SCRIPTS_NOTICE = (
    "it has scripts, which run sandboxed (no storage, no reading the site's files, no "
    "forms, popups or alerts): tell the user what they do and ask before publishing it"
)
# What breaks in the sandbox, looked for in a page with scripts and in its .js files.
_SCRIPT_LIMITS = (
    (
        (
            "localStorage, sessionStorage, IndexedDB and cookies throw; keep its state "
            "in the page"
        ),
        re.compile(r"\b(?:localStorage|sessionStorage|indexedDB|document\.cookie)\b"),
    ),
    (
        (
            "fetch and XMLHttpRequest can't read the site's files (or another host's); "
            "put the data in the page"
        ),
        re.compile(r"\bfetch\(|\bXMLHttpRequest\b"),
    ),
    (
        "module scripts can't load files (src= or import); use plain scripts",
        re.compile(
            r"""(?i:<script\b[^<>]*\btype\s*=\s*["']?module)"""
            r"""|\bimport\(|^\s*import\b\s*[\w{*"']""",
            re.MULTILINE,
        ),
    ),
    (
        "alert, confirm and prompt do nothing; show messages in the page",
        re.compile(r"\b(?:alert|confirm|prompt)\("),
    ),
)
# What breaks in the sandbox with or without scripts.
_PAGE_LIMITS = (
    (
        "links with target=_blank won't open (no new tabs); drop the target",
        re.compile(r"""\btarget\s*=\s*["']?_blank""", re.IGNORECASE),
    ),
    (
        (
            "forms don't submit, and their submit event never fires; use a button's "
            "click handler"
        ),
        re.compile(r"<form\b", re.IGNORECASE),
    ),
)
NOTICES = (
    SCRIPTS_NOTICE,
    *(what for what, _ in _SCRIPT_LIMITS),
    *(what for what, _ in _PAGE_LIMITS),
)


def page_notices(text: str, script: bool = False) -> list[str]:
    """What the sandbox the pages site runs scripts in means for a page's HTML, or with
    `script`, for one of its .js files: [] when nothing does."""
    scripts = script or bool(SCRIPTS_RE.search(text))
    found = [] if script else [w for w, pattern in _PAGE_LIMITS if pattern.search(text)]
    if scripts and not script:
        found.append(SCRIPTS_NOTICE)
    if scripts:
        found += [w for w, pattern in _SCRIPT_LIMITS if pattern.search(text)]
    return [n for n in NOTICES if n in found]


def page_description(html: str) -> str:
    """A line about a page for its card: its meta description, else its first paragraph."""
    if m := DESCRIPTION_RE.search(html):
        return htmllib.unescape(m.group(2))
    for m in PARAGRAPH_RE.finditer(html):
        if text := " ".join(htmllib.unescape(TAG_RE.sub(" ", m.group(1))).split()):
            return text
    return ""


@dataclass
class Pages:
    """Every workspace's pages, on the sites the config names; the runner calls these
    under the workspace's lock."""

    config: Config

    def public_url(self, workspace: str, path: str = "") -> str:
        """Where `path` in a workspace's /public is on the workspace pages site."""
        return f"{self.config.public_url.rstrip('/')}/{workspace}/{path}"

    def page_url(self, workspace: str, item: Path) -> str:
        """A top-level entry of /public on the site: a folder as itself, a file as the file."""
        return self.public_url(
            workspace, quote(item.name) + ("/" if item.is_dir() else "")
        )

    def checked(self, item: Path) -> dict[str, list[str]]:
        """What in a page the site's CSP blocks, so the agent hears it won't load, and what
        it should know about the sandbox the page's scripts run in (`notices`)."""
        origin = "/".join(self.config.public_url.split("/")[:3])
        files = [
            (f, name.endswith((".js", ".mjs")))
            for f, name in regular_files(item)[0]
            if name.endswith((".html", ".htm", ".js", ".mjs"))
        ]
        blocked, notices = set(), set()
        for f, script in files[:CSP_SCAN]:
            data = safefs.read_regular(f.parent, (f.name,), WRITE_BYTES)
            if data is not None:
                text = data.decode(errors="replace")
                if not script:
                    blocked.update(csp_blocked(text, origin))
                notices.update(page_notices(text, script))
        return {
            "blocked": sorted(blocked),
            "notices": [n for n in NOTICES if n in notices],
        }

    def page_changes(self, scope: Scope, names: set[str]) -> dict[str, Any] | None:
        """Where the top-level entries `names` of /public that changed are now, with what
        their CSP blocks and their notices (`checked`), and which of them are gone; None
        when there are none. Hidden entries are left out: the site doesn't serve them."""
        live, removed = [], []
        for name in sorted(n for n in names if not n.startswith(".")):
            item = scope.public / name
            if not os.path.lexists(item):
                removed.append(name)
            elif not item.is_symlink() and (item.is_dir() or item.is_file()):
                live.append(
                    {
                        "slug": name,
                        "url": self.page_url(scope.workspace, item),
                        **self.checked(item),
                    }
                )
        result = {"live": live, "removed": removed}
        return {k: v for k, v in result.items() if v} or None

    def listing(self, scope: Scope) -> dict[str, Any]:
        """The workspace's pages: /public's top-level entries and where they are."""
        items = sorted(e for e in scope.public.iterdir() if not e.name.startswith("."))
        return {
            "site": self.public_url(scope.workspace),
            "pages": [
                {"slug": e.name, "url": self.page_url(scope.workspace, e)}
                for e in items[:LIST_MAX]
            ],
        }

    def page_info(self, workspace: str, item: Path) -> dict[str, Any]:
        """A page's address, size, what its CSP blocks, its notices and its link card."""
        url = self.page_url(workspace, item)
        # Read without following a symlink a run left: index.html could point at another
        # workspace's files, whose title and description would go on the card.
        entry = (item, "index.html") if item.is_dir() else (item.parent, item.name)
        title, description = item.name, ""
        data = None
        if entry[1].lower().endswith((".html", ".htm")):
            data = safefs.read_regular(entry[0], (entry[1],), WRITE_BYTES)
        if data is not None:
            html = data.decode(errors="replace")
            if m := TITLE_RE.search(html):
                title = htmllib.unescape(" ".join(m.group(1).split())) or title
            description = page_description(html)
        return {
            "slug": item.name,
            "url": url,
            "files": len(regular_files(item)[0]),
            **self.checked(item),
            "card": chatimage.card.make(
                self.config.site_dir,
                url,
                title,
                f"Pages · {workspace}",
                description,
                images=self.config.site_url,
            ),
        }

    def public_entries(self, public: Path, slug: str) -> list[Path]:
        """/public's entries for `slug`: its folder, or a file named <slug>.<ext>."""
        if not public.is_dir():
            return []
        return [
            e
            for e in public.iterdir()
            if e.name == slug or (Path(e.name).stem == slug and "." in e.name)
        ]

    def stage(self, scope: Scope, path: str, slug: str) -> None:
        """Copy a file or folder from the workspace's own folders to /public/<slug>, in place
        of whatever was there: an HTML file as its index.html, a folder whole (plain files
        only)."""
        source = resolve(scope, path)
        if not source.exists():
            raise SandboxError(f"there's no '{path}'")
        files, size = regular_files(source)
        if not files:
            raise SandboxError(f"'{path}' has no files to publish")
        if size > PUBLISH_MAX_BYTES:
            raise SandboxError(
                f"'{path}' is {size >> 20} MB; the most publish copies is {PUBLISH_MAX_BYTES >> 20} MB"
            )
        self.put_public(scope, files, slug)

    def stage_files(self, scope: Scope, source: Path, slug: str) -> int:
        """A build's output into /public/<slug>: its plain files, within the copy cap."""
        files, size = regular_files(source) if source.is_dir() else ([], 0)
        if not files:
            raise SandboxError("the build produced no files")
        if size > PUBLISH_MAX_BYTES:
            raise SandboxError(
                f"the built site is {size >> 20} MB; a build can copy at most "
                f"{PUBLISH_MAX_BYTES >> 20} MB"
            )
        self.put_public(scope, files, slug)
        return len(files)

    def put_public(
        self, scope: Scope, files: list[tuple[Path, str]], slug: str
    ) -> None:
        """Copy plain files into /public/<slug>, in place of whatever was there for it, built
        in a hidden folder beside it (which the site doesn't serve) and swapped in."""
        new = Path(tempfile.mkdtemp(dir=scope.public, prefix=f".{slug}."))
        try:
            for src, name in files:
                (new / name).parent.mkdir(parents=True, exist_ok=True)
                copy_regular(src, new / name)
            new.chmod(0o755)
            for old in self.public_entries(scope.public, slug):
                remove_path(old)
            os.rename(new, scope.public / slug)
        except BaseException:
            remove_path(new)
            raise

    def read_image(self, scope: Scope, path: str) -> bytes:
        """The bytes of the plain file at `path`, at most IMAGE_MAX_BYTES."""
        source = resolve(scope, path)
        try:
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            raise SandboxError(f"there's no '{path}'") from None
        except OSError as e:
            raise SandboxError(f"'{path}' can't be read ({e.strerror})") from None
        if not stat.S_ISREG(
            os.fstat(fd).st_mode
        ):  # before fdopen, which refuses a folder
            os.close(fd)
            raise SandboxError(f"'{path}' isn't a file")
        with os.fdopen(fd, "rb") as f:
            data = f.read(IMAGE_MAX_BYTES + 1)
        if len(data) > IMAGE_MAX_BYTES:
            raise SandboxError(
                f"'{path}' is over {IMAGE_MAX_BYTES >> 20} MB; make it smaller with run-code"
            )
        return data

    def put_image(
        self, workspace: str, data: bytes, path: str, alt: str
    ) -> dict[str, Any]:
        """Save an image on the pages site, by its hash, and trim the workspace's to
        IMAGES_MAX_BYTES, oldest shown first."""
        try:  # the header only: nothing is decoded
            with Image.open(io.BytesIO(data)) as image:
                kind, (width, height) = image.format, image.size
        except Exception:  # noqa: BLE001 - whatever Pillow can't read isn't one
            kind, width, height = None, 0, 0
        ext = IMAGE_FORMATS.get(kind or "")
        if ext is None:
            raise SandboxError(
                f"'{path}' isn't a PNG, JPEG, GIF or WebP image; convert it with "
                "run-code (an SVG with cairosvg, say), or publish it as a page"
            )
        name = f"{hashlib.sha256(data).hexdigest()[:32]}.{ext}"
        self.config.site_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        folder = safefs.open_dir(self.config.site_dir, (IMAGES, workspace), make=True)
        try:
            try:  # shown before: only its time, so the trim keeps it
                os.utime(name, dir_fd=folder, follow_symlinks=False)
                shown = stat.S_ISREG(
                    os.stat(name, dir_fd=folder, follow_symlinks=False).st_mode
                )
            except FileNotFoundError:
                shown = False
            if not shown:
                safefs.replace(folder, name, data)
            trim_images(folder, IMAGES_MAX_BYTES)
        finally:
            os.close(folder)
        url = f"{self.config.site_url.rstrip('/')}/{IMAGES}/{quote(workspace)}/{name}"
        label = " ".join(alt.split()) or Path(path).stem or "image"
        return {
            "url": url,
            "width": width,
            "height": height,
            "bytes": len(data),
            "image": chatimage.linked_image(label, url, url),
        }
