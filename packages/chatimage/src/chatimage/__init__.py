"""Chat images: pictures the host draws for the chat, which the agent shows as Markdown.

AnythingLLM's chat renders Markdown and shows an image up to 800 px wide, whether or not
"Render HTML in chat" is on (that setting is per browser and off by default, and what HTML
it lets through is sanitized: no scripts, no handlers, no iframes). An image inside a link
stays a link. So anything richer than text that has to show everywhere the chat does is
an image the host draws:

- `chatimage.card`: a link card for a published page, saved on the pages site.
- `chatimage.progress`: a frame of a progress card for a long job.
- `chatimage.live`: serves frames as `multipart/x-mixed-replace`, so an `<img>` keeps
  showing the newest one while the connection stays open: a live image with no script.

This module is the kit they share: the fonts, the palette, and fitting text to a width.

No config: the fonts are the host's DejaVu or Liberation Sans, else Pillow's own.
"""

import hashlib
import os
import re
import tempfile
import unicodedata
from pathlib import Path

from PIL import ImageDraw, ImageFont

WIDTH = 1600  # drawn at twice the size the chat shows it
PAD = 80
RADIUS = 36
BAR = 14  # the accent stripe down the left edge

BACKGROUND = (29, 32, 39)
TITLE = (240, 242, 246)
MUTED = (170, 176, 188)
FAINT = (125, 133, 144)
TRACK = (52, 57, 68)  # behind a progress bar
GOOD = (99, 210, 151)
BAD = (240, 113, 120)
# One per site, picked by the label's first part, so a site's images share a colour.
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


def accent_for(label: str) -> tuple[int, int, int]:
    """The accent colour for a label, picked by its first part (before " · ")."""
    digest = hashlib.sha256(label.split(" · ")[0].encode()).hexdigest()
    return ACCENTS[int(digest, 16) % len(ACCENTS)]


def frame(d: ImageDraw.ImageDraw, height: int, accent) -> None:
    """The rounded dark panel with the accent stripe down its left edge."""
    d.rounded_rectangle((0, 0, WIDTH - 1, height - 1), RADIUS, fill=accent)
    d.rounded_rectangle(
        (BAR, 0, WIDTH - 1, height - 1),
        RADIUS,
        fill=BACKGROUND,
        corners=(False, True, True, False),
    )


def alt(text: str) -> str:
    """Text that can sit in a Markdown image's square brackets."""
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
