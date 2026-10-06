"""Link cards: a big clickable picture for a page the agent links in the chat, like the
Claude app's artifact cards.

When a page is published (sandbox.runner's publish, the sites tools, deep research), or the
agent asks for a site or an entry that's already there (the sites tools' list_sites,
list_entries and get_entry), the host draws a card with the page's title, where it lives
and a line about it, saves it on the pages site in `_cards/`, and hands the agent a line to
paste as is:

    [![Title](https://<host>:8445/_cards/<name>.png?v=<hash>)](<page url>)

A card's name comes from its page's path, so republishing a page replaces its card. ?v=
is a hash of what the card says (kept in the PNG too), so a card whose text hasn't changed
isn't drawn again and keeps its URL, and one that has gets a new URL, so the chat doesn't
show an old card from its cache. No page or site can be called `_cards`: their slugs start
with a letter or digit.
"""

import hashlib
import io
import json
import logging
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from PIL import Image, ImageDraw
from PIL.PngImagePlugin import PngInfo

from chatimage import (
    BAR,
    FAINT,
    MUTED,
    PAD,
    TITLE,
    WIDTH,
    accent_for,
    alt,
    clean,
    fit,
    font,
    frame,
    link,
    wrap,
    write_atomic,
)

log = logging.getLogger("chatimage.card")

FOLDER = "_cards"
DESIGN = 1  # part of every card's hash: raise it when the drawing changes, to redraw them all
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
    file = card_path(site_dir, url)
    try:
        if drawn(file) != version:
            png = draw(title, label, description, where, version)
            file.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            write_atomic(file, png)
    except Exception as e:  # noqa: BLE001 - a card is a nicety; the caller still gives the link
        log.warning("couldn't make a card for %s: %s", url, e)
        return ""
    image = urljoin(images or url, f"/{FOLDER}/{file.name}?v={version}")
    return f"[![{alt(title)}]({link(image)})]({link(url)})"


def remove(site_dir: Path, url: str) -> None:
    """Delete the card for the page at `url`, if it has one."""
    card_path(site_dir, url).unlink(missing_ok=True)


def drawn(file: Path) -> str | None:
    """The version of the card in `file`, from its PNG text; None when there's none."""
    try:
        with Image.open(file) as image:
            return getattr(image, "text", {}).get("card")
    except (OSError, ValueError):
        return None


def card_path(site_dir: Path, url: str) -> Path:
    name = hashlib.sha256(urlsplit(url).path.encode()).hexdigest()[:20]
    return site_dir / FOLDER / f"{name}.png"


def shown_url(url: str) -> str:
    """The URL as the card prints it: host and path, without the scheme or a closing /."""
    parts = urlsplit(url)
    return (parts.netloc + parts.path).rstrip("/")


def draw(
    title: str, label: str, description: str, where: str, version: str = ""
) -> bytes:
    """The card as a PNG: label, title (up to 3 lines), description in what's left, and
    the URL along the bottom."""
    title, label, description, where = map(clean, (title, label, description, where))
    accent = accent_for(label)
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    frame(d, HEIGHT, accent)

    width = WIDTH - BAR - 2 * PAD
    x = BAR + PAD
    small, body, big = font("regular", 32), font("regular", 36), font("bold", 62)

    y = 54
    d.text((x, y), fit(d, label, small, width), font=small, fill=accent)
    y += 62
    lines = wrap(d, title or where, big, width, 3 if not description else 2)
    for line in lines:
        d.text((x, y), line, font=big, fill=TITLE)
        y += 76
    room = 3 - len(lines)
    if description and room:
        y += 14
        for line in wrap(d, description, body, width, room):
            d.text((x, y), line, font=body, fill=MUTED)
            y += 48
    d.text(
        (x, HEIGHT - 54 - 32),
        fit(d, f"→ {where}", small, width),
        font=small,
        fill=FAINT,
    )

    out = io.BytesIO()
    info = PngInfo()
    info.add_text("card", version)
    image.save(out, "PNG", optimize=True, pnginfo=info)
    return out.getvalue()
