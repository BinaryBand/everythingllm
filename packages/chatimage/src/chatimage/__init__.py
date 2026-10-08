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

This module is the kit they share: the fonts, the palettes, and fitting text to a width.

Every image comes in two themes, dark and light, so a client can show the one that matches
its own: dark unless asked otherwise (AnythingLLM's chat always gets dark), light when the
image's address says `theme=light` (`live.theme`, and the pages site's Caddyfile for the
saved link cards). The light accents are the dark ones darkened to OKLCH lightness 0.5,
so every text colour keeps a 4.5:1 contrast on its panel.

No config: the fonts are the host's DejaVu or Liberation Sans, else Pillow's own.
"""

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass

from PIL import ImageDraw, ImageFont
from PIL.ImageColor import getrgb

WIDTH = 1600  # drawn at twice the size the chat shows it
PAD = 80
RADIUS = 36
BAR = 14  # the accent stripe down the left edge
EDGE = 2  # the panel's outline, so a card stands off a background of its own colour

RGB = tuple[int, ...]


@dataclass(frozen=True)
class Palette:
    panel: RGB
    line: RGB  # the panel's outline
    title: RGB
    text: RGB
    faint: RGB  # an address, a note, a closed browser tab
    track: RGB  # behind a progress bar
    done: RGB
    failed: RGB
    user: RGB  # a browser card's strip while the user has the browser
    # One per site, picked by the label's first part, so a site's images share a colour.
    accents: tuple[RGB, ...]


THEMES = {
    "dark": Palette(
        panel=getrgb("#22232E"),
        line=getrgb("#3B3C49"),
        title=getrgb("#ECE7E1"),
        text=getrgb("#C6C0B7"),
        faint=getrgb("#A2A4B0"),
        track=getrgb("#3B3C49"),
        done=getrgb("#68D7A1"),
        failed=getrgb("#F68482"),
        user=getrgb("#F2B772"),
        accents=tuple(
            map(
                getrgb,
                ("#79B6F4", "#68D7A1", "#F2B772", "#F496BB", "#BCA8FD", "#64D1D7"),
            )
        ),
    ),
    "light": Palette(
        panel=getrgb("#FFFDFB"),
        line=getrgb("#E2DBD2"),
        title=getrgb("#242533"),
        text=getrgb("#454655"),
        faint=getrgb("#61626F"),
        track=getrgb("#EDE5DB"),
        done=getrgb("#007047"),
        failed=getrgb("#AF2934"),
        user=getrgb("#8B5511"),
        accents=tuple(
            map(
                getrgb,
                ("#0465AF", "#007047", "#8B5511", "#A13567", "#6A4DAF", "#02717A"),
            )
        ),
    ),
}
THEME = "dark"  # the theme an image is drawn in unless its address asks for another


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


def accent_for(label: str, p: Palette) -> RGB:
    """The accent colour for a label, picked by its first part (before " · ")."""
    accents = p.accents
    digest = hashlib.sha256(label.split(" · ")[0].encode()).hexdigest()
    return accents[int(digest, 16) % len(accents)]


def frame(d: ImageDraw.ImageDraw, height: int, accent, p: Palette) -> None:
    """The rounded outlined panel with the accent stripe down its left edge."""
    d.rounded_rectangle((0, 0, WIDTH - 1, height - 1), RADIUS, fill=accent)
    d.rounded_rectangle(
        (BAR, 0, WIDTH - 1, height - 1),
        RADIUS,
        fill=p.panel,
        outline=p.line,
        width=EDGE,
        corners=(False, True, True, False),
    )


def alt(text: str) -> str:
    """Text that can sit in a Markdown image's square brackets."""
    return re.sub(r"([\[\]\\])", r"\\\1", " ".join(text.split()))


def link(url: str) -> str:
    """A URL that can sit in a Markdown link's parentheses."""
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


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


def longest(d: ImageDraw.ImageDraw, text: str, f, width: int, end: str = "") -> int:
    """How much of `text` fits in `width` with `end` after it: found by halving, since a
    card's text can be a model's or an error's, thousands of characters long, and
    measuring it one character shorter each time took a minute."""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if d.textlength(text[:mid] + end, font=f) <= width:
            lo = mid
        else:
            hi = mid - 1
    return lo


def fit(d: ImageDraw.ImageDraw, text: str, f, width: int) -> str:
    """`text`, cut short with … if it's wider than `width`."""
    if d.textlength(text, font=f) <= width:
        return text
    return text[: longest(d, text, f, width, "…")].rstrip() + "…"


def wrap(d: ImageDraw.ImageDraw, text: str, f, width: int, most: int) -> list[str]:
    """`text` in at most `most` lines no wider than `width`, the last cut short with … when
    there's more."""
    lines: list[str] = []
    words = text.split()
    while words and len(lines) < most:
        line = words.pop(0)
        if d.textlength(line, font=f) > width:  # a word too long for a line on its own
            cut = max(1, longest(d, line, f, width))
            words.insert(0, line[cut:])
            line = line[:cut]
        while words and d.textlength(f"{line} {words[0]}", font=f) <= width:
            line += f" {words.pop(0)}"
        lines.append(line)
    if words and lines:
        lines[-1] = fit(d, f"{lines[-1]} {words[0]}", f, width)
    return lines
