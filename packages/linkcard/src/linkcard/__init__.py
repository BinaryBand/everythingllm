"""Link cards: a big clickable picture for a page the agent links in the chat, like the
Claude app's artifact cards.

AnythingLLM's chat renders Markdown and shows an image up to 800 px wide, whether or not
"Render HTML in chat" is on, and an image inside a link stays a link. So when a page is
published (sandbox.runner's publish, the sites tools, deep research), or the agent asks
for a site or an entry that's already there (the sites tools' list_sites, list_entries and
get_entry), the host draws a card with the page's title, where it lives and a line about
it, saves it on the pages site in `_cards/`, and hands the agent a line to paste as is:

    [![Title](https://<host>:8445/_cards/<name>.png?v=<hash>)](<page url>)

A card's name comes from its page's path, so republishing a page replaces its card. ?v=
is a hash of what the card says (kept in the PNG too), so a card whose text hasn't changed
isn't drawn again and keeps its URL, and one that has gets a new URL, so the chat doesn't
show an old card from its cache. No page or site can be called `_cards`: their slugs start
with a letter or digit.

No config: the fonts are the host's DejaVu or Liberation Sans, else Pillow's own.
"""

import hashlib
import io
import json
import logging
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from PIL import Image, ImageDraw, ImageFont
from PIL.PngImagePlugin import PngInfo

log = logging.getLogger("linkcard")

FOLDER = "_cards"
DESIGN = 1  # part of every card's hash: raise it when the drawing changes, to redraw them all
WIDTH, HEIGHT = 1600, 460  # drawn at twice the size the chat shows it
PAD = 80
RADIUS = 36
BAR = 14  # the accent stripe down the left edge

BACKGROUND = (29, 32, 39)
TITLE = (240, 242, 246)
MUTED = (170, 176, 188)
FAINT = (125, 133, 144)
# One per site, picked by the label's first part, so a site's cards share a colour.
ACCENTS = (
    (110, 168, 254),
    (99, 210, 151),
    (242, 181, 107),
    (240, 143, 179),
    (178, 155, 248),
    (92, 207, 214),
)

FONTS = {
    "regular": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ),
    "bold": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ),
}


def make(
    site_dir: Path, url: str, title: str, label: str, description: str = ""
) -> str:
    """Draw the card for the page at `url`, save it in the pages site's `_cards/`, and
    return the Markdown line that shows it as a link to the page; "" when it couldn't be
    made, since the page is published either way."""
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
    image = urljoin(url, f"/{FOLDER}/{file.name}?v={version}")
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


def alt(text: str) -> str:
    return re.sub(r"([\[\]\\])", r"\\\1", " ".join(text.split()))


def link(url: str) -> str:
    """A URL that can sit in a Markdown link's parentheses."""
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def write_atomic(file: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=f".{file.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, file)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- drawing ---


def draw(
    title: str, label: str, description: str, where: str, version: str = ""
) -> bytes:
    """The card as a PNG: label, title (up to 3 lines), description in what's left, and
    the URL along the bottom."""
    title, label, description, where = map(clean, (title, label, description, where))
    accent = ACCENTS[
        int(hashlib.sha256(label.split(" · ")[0].encode()).hexdigest(), 16)
        % len(ACCENTS)
    ]
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    d.rounded_rectangle((0, 0, WIDTH - 1, HEIGHT - 1), RADIUS, fill=accent)
    d.rounded_rectangle(
        (BAR, 0, WIDTH - 1, HEIGHT - 1),
        RADIUS,
        fill=BACKGROUND,
        corners=(False, True, True, False),
    )

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


def clean(text: str) -> str:
    """One line of text the fonts can draw: no emoji or other symbols they'd show as boxes."""
    kept = (
        c
        for c in text
        if unicodedata.category(c) not in ("So", "Cs", "Co", "Cn")
        and not 0xFE00 <= ord(c) <= 0xFE0F
        and c != "‍"
    )
    return " ".join("".join(kept).split())


def font(weight: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONTS[weight]:
        if os.path.isfile(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def fit(d: ImageDraw.ImageDraw, text: str, f, width: int) -> str:
    """`text`, cut short with … if it's wider than `width`."""
    if d.textlength(text, font=f) <= width:
        return text
    while text and d.textlength(text + "…", font=f) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def wrap(d: ImageDraw.ImageDraw, text: str, f, width: int, most: int) -> list[str]:
    """`text` in at most `most` lines no wider than `width`, the last cut short with … when
    there's more."""
    lines: list[str] = []
    words = text.split()
    while words and len(lines) < most:
        line = words.pop(0)
        if d.textlength(line, font=f) > width:  # a word too long for a line on its own
            cut = len(line)
            while cut > 1 and d.textlength(line[:cut], font=f) > width:
                cut -= 1
            words.insert(0, line[cut:])
            line = line[:cut]
        while words and d.textlength(f"{line} {words[0]}", font=f) <= width:
            line += f" {words.pop(0)}"
        lines.append(line)
    if words and lines:
        lines[-1] = fit(d, f"{lines[-1]} {words[0]}", f, width)
    return lines
