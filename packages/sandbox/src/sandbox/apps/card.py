"""An app's card for the chat, as chatimage draws every card: a picture of its page
(`pictured`, sandbox.appshot), or, until there is one or when it can't be taken, drawn from
its data (`draw`).

What `draw` shows is the app's `card` (sandbox.apps): rows with a checkbox each, read from a
list in its data, or lines of text the page or the agent wrote; open rows come first, done
ones struck through, at most ROWS and a count of the rest.
"""

from __future__ import annotations

import io

from chatimage import BAR, PAD, RADIUS, THEMES, WIDTH, clean, fit, font, frame
from PIL import Image, ImageDraw

ROWS = 6
MOST_SIDE = 4000  # a picture's width or height, at most, before it's decoded


def draw(
    title: str, summary: str, rows: list[tuple[str, bool | None]], theme: str
) -> bytes:
    """The card: `rows` are (text, done), done None for a line with no checkbox."""
    p = THEMES[theme]
    accent = p.app
    rows = [r for r in rows if r[1] is not True] + [r for r in rows if r[1] is True]
    shown, rest = rows[:ROWS], len(rows) - ROWS
    small, body, big = font("regular", 32), font("regular", 40), font("bold", 58)
    row_h, row_gap = 48, 18
    height = 52 + 38 + 24 + 70 + 28 + 46  # label, title, footer
    height += max(1, len(shown)) * (row_h + row_gap) + (row_h if rest > 0 else 0) + 40
    image = Image.new("RGBA", (WIDTH, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    frame(d, height, accent, p)
    x, width = BAR + PAD, WIDTH - BAR - 2 * PAD
    y = 52
    label = f"App · {summary}" if summary else "App"
    d.text((x, y), fit(d, clean(label), small, width), font=small, fill=accent)
    y += 38 + 24
    d.text((x, y), fit(d, clean(title), big, width), font=big, fill=p.title)
    y += 70 + 28
    if not shown:
        d.text((x, y), "Nothing here yet.", font=body, fill=p.faint)
        y += row_h + row_gap
    for text, done in shown:
        tx = x
        if done is not None:
            box = (x, y + 2, x + 44, y + 46)
            if done:
                d.rounded_rectangle(box, 10, fill=accent)
                d.line(
                    [(x + 10, y + 25), (x + 19, y + 34), (x + 35, y + 14)],
                    fill=p.panel,
                    width=6,
                    joint="curve",
                )
            else:
                d.rounded_rectangle(box, 10, outline=p.faint, width=4)
            tx = x + 72
        line = fit(d, clean(text), body, width - (tx - x))
        d.text((tx, y), line, font=body, fill=p.faint if done else p.title)
        if done:
            mid = y + 26
            d.line(
                [(tx, mid), (tx + d.textlength(line, font=body), mid)],
                fill=p.faint,
                width=3,
            )
        y += row_h + row_gap
    if rest > 0:
        d.text((x, y), f"+ {rest} more", font=small, fill=p.faint)
        y += row_h
    return footed(d, image, height, p)


def pictured(title: str, shot: bytes, theme: str) -> bytes:
    """The card with `shot`, a picture of the app's page, under its title: scaled to the
    card's width and cut to no taller than it is wide. ValueError when `shot` isn't a
    picture of a usable size, which Pillow decodes only once its header says so."""
    p = THEMES[theme]
    try:
        with Image.open(io.BytesIO(shot)) as raw:
            if raw.format != "PNG" or max(raw.size) > MOST_SIDE:
                raise ValueError("not a picture of the page")
            picture = raw.convert("RGB")
    except OSError as e:  # not an image, or a broken one
        raise ValueError(f"not a picture of the page: {e}") from None
    x, width = BAR + PAD, WIDTH - BAR - 2 * PAD
    scaled = round(picture.height * width / picture.width)
    picture = picture.resize((width, scaled), Image.Resampling.LANCZOS)
    picture = picture.crop((0, 0, width, min(scaled, width)))
    small = font("regular", 32)
    top = 52 + 38 + 30
    height = top + picture.height + 40 + 32 + 46
    image = Image.new("RGBA", (WIDTH, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    frame(d, height, p.app, p)
    label = f"App · {named}" if (named := clean(title)) else "App"
    d.text((x, 52), fit(d, label, small, width), font=small, fill=p.app)
    corners = Image.new("L", picture.size, 0)
    ImageDraw.Draw(corners).rounded_rectangle(
        (0, 0, picture.width - 1, picture.height - 1), RADIUS // 2, fill=255
    )
    image.paste(picture, (x, top), corners)
    d.rounded_rectangle(
        (x, top, x + picture.width - 1, top + picture.height - 1),
        RADIUS // 2,
        outline=p.line,
        width=2,
    )
    return footed(d, image, height, p)


def footed(d: ImageDraw.ImageDraw, image: Image.Image, height: int, p) -> bytes:
    """`image`, `height` tall, with the footer every app card has, as a PNG."""
    small = font("regular", 32)
    d.text((BAR + PAD, height - 46 - 32), "→ tap to open", font=small, fill=p.faint)
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue()
