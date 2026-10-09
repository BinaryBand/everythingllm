"""An app's card for the chat, drawn from its data as chatimage draws every card.

What it shows is the app's `card` (sandbox.apps): rows with a checkbox each, read from a list
in its data, or lines of text the page or the agent wrote; open rows come first, done ones
struck through, at most ROWS and a count of the rest.
"""

from __future__ import annotations

import io

from chatimage import BAR, PAD, THEMES, WIDTH, clean, fit, font, frame
from PIL import Image, ImageDraw

ROWS = 6


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
    d.text((x, height - 46 - 32), "→ tap to open", font=small, fill=p.faint)
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue()
