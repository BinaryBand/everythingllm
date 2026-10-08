"""Link cards: a big clickable picture for a page the agent links in the chat, like the
Claude app's artifact cards.

When a page is published (sandbox.runner's publish), the host draws a card with the page's
title, where it lives and a line about it, saves it on the pages site in `_cards/`, and
hands the agent a line to paste as is:

    [![Title](https://<host>:8445/_cards/<name>.png?v=<hash>)](<page url>)

A card's name comes from its page's path, so republishing a page replaces its card. ?v=
is a hash of what the card says (kept in the PNG too), so a card whose text hasn't changed
isn't drawn again and keeps its URL, and one that has gets a new URL, so the chat doesn't
show an old card from its cache. Each card is saved twice, `<name>.png` in the dark theme and
`<name>.light.png` in the light one; the pages site's Caddyfile serves the light one when
the address asks for it (`&theme=light` after the ?v=), and the dark one for a card drawn
before there were two. A card is read and written without following a symlink (`save`,
`drawn`): service containers could once write the pages site, and a symlink one left there
mustn't send the host's write elsewhere.
"""

import contextlib
import hashlib
import io
import json
import logging
import os
import stat
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from PIL import Image, ImageDraw
from PIL.PngImagePlugin import PngInfo

from chatimage import (
    BAR,
    PAD,
    THEME,
    THEMES,
    WIDTH,
    accent_for,
    alt,
    clean,
    fit,
    font,
    frame,
    link,
    wrap,
)

log = logging.getLogger("chatimage.card")

FOLDER = "_cards"
DESIGN = 2  # part of every card's hash: raise it when the drawing changes, to redraw them all
HEIGHT = 460


def make(
    site_dir: Path,
    url: str,
    title: str,
    label: str,
    description: str = "",
    images: str = "",
) -> str:
    """Draw the card for the page at `url`, save it in the pages site's `_cards/`, and
    return the Markdown line that shows it as a link to the page; "" when it couldn't be
    made, since the page is published either way. `images` is the pages site's URL, for a
    page served elsewhere (a workspace's, on its own port); by default the page's own host."""
    where = shown_url(url)
    version = hashlib.sha256(
        json.dumps([DESIGN, title, label, description, where]).encode()
    ).hexdigest()[:10]
    files = {theme: card_path(site_dir, url, theme) for theme in THEMES}
    try:
        for theme, file in files.items():
            if drawn(file) != version:
                png = draw(title, label, description, where, version, theme)
                save(site_dir, file.name, png)
    except Exception as e:  # noqa: BLE001 - a card is a nicety; the caller still gives the link
        log.warning("couldn't make a card for %s: %s", url, e)
        return ""
    image = urljoin(images or url, f"/{FOLDER}/{files[THEME].name}?v={version}")
    return f"[![{alt(title)}]({link(image)})]({link(url)})"


def remove(site_dir: Path, url: str) -> None:
    """Delete the card for the page at `url`, if it has one, in every theme."""
    for theme in THEMES:
        card_path(site_dir, url, theme).unlink(missing_ok=True)


DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def drawn(file: Path) -> str | None:
    """The version of the card in `file`, from its PNG text; None when there's none (or it
    isn't a plain file)."""
    try:
        fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return None
            with Image.open(f) as image:
                return image.info.get("card")  # a chunk before the pixels: no decoding
    except (OSError, ValueError):
        return None


def save(site_dir: Path, name: str, png: bytes) -> None:
    """Put a card in `site_dir`'s _cards/ in one step: the folder is made and opened
    relative to the site's, and the file goes in through a temp file of its own, so a
    symlink in either place is refused or replaced, never followed."""
    site_dir.mkdir(mode=0o755, parents=True, exist_ok=True)  # the caller's own
    site = os.open(site_dir, DIR_FLAGS)
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(FOLDER, 0o755, dir_fd=site)
        folder = os.open(FOLDER, DIR_FLAGS, dir_fd=site)
    finally:
        os.close(site)
    tmp = f".{name}.{os.urandom(4).hex()}.tmp"
    try:
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
            dir_fd=folder,
        )
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o644)
            f.write(png)
        os.replace(tmp, name, src_dir_fd=folder, dst_dir_fd=folder)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=folder)
        raise
    finally:
        os.close(folder)


def card_path(site_dir: Path, url: str, theme: str = THEME) -> Path:
    name = hashlib.sha256(urlsplit(url).path.encode()).hexdigest()[:20]
    return (
        site_dir / FOLDER / (f"{name}.png" if theme == THEME else f"{name}.{theme}.png")
    )


def shown_url(url: str) -> str:
    """The URL as the card prints it: host and path, without the scheme or a closing /."""
    parts = urlsplit(url)
    return (parts.netloc + parts.path).rstrip("/")


def draw(
    title: str,
    label: str,
    description: str,
    where: str,
    version: str = "",
    theme: str = THEME,
) -> bytes:
    """The card as a PNG: label, title (up to 3 lines), description in what's left, and
    the URL along the bottom."""
    title, label, description, where = map(clean, (title, label, description, where))
    p = THEMES[theme]
    accent = accent_for(label, p)
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    frame(d, HEIGHT, accent, p)

    width = WIDTH - BAR - 2 * PAD
    x = BAR + PAD
    small, body, big = font("regular", 32), font("regular", 36), font("bold", 62)

    y = 54
    d.text((x, y), fit(d, label, small, width), font=small, fill=accent)
    y += 62
    lines = wrap(d, title or where, big, width, 3 if not description else 2)
    for line in lines:
        d.text((x, y), line, font=big, fill=p.title)
        y += 76
    room = 3 - len(lines)
    if description and room:
        y += 14
        for line in wrap(d, description, body, width, room):
            d.text((x, y), line, font=body, fill=p.text)
            y += 48
    d.text(
        (x, HEIGHT - 54 - 32),
        fit(d, f"→ {where}", small, width),
        font=small,
        fill=p.faint,
    )

    out = io.BytesIO()
    info = PngInfo()
    info.add_text("card", version)
    image.save(out, "PNG", optimize=True, pnginfo=info)
    return out.getvalue()
