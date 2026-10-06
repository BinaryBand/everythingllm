"""Progress cards: a frame of a long job's progress, in the link card's style, for
chatimage.live to push to the chat as the job goes on.

A frame has the job's label (what it is and how it stands), its title, a bar and the
latest thing it did. `state` sets the colours: "running" in the label's accent, "done" in
green with a full bar, "failed" and "interrupted" in red. A running job that can't say
how far along it is (a queued one) gets a striped bar instead of a fraction.
"""

import io

from PIL import Image, ImageChops, ImageDraw

from chatimage import (
    BAD,
    BAR,
    GOOD,
    MUTED,
    PAD,
    TITLE,
    TRACK,
    WIDTH,
    accent_for,
    clean,
    fit,
    font,
    frame,
    wrap,
)

HEIGHT = 400
STATES = ("running", "done", "failed", "interrupted")
BAR_HEIGHT = 28
STRIPE = 36  # the striped bar's period


def draw(
    title: str,
    label: str,
    fraction: float | None = None,
    line: str = "",
    state: str = "running",
) -> bytes:
    """The frame as a PNG: label, title (up to 2 lines), the bar with its percentage, and
    `line` under it."""
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}")
    title, label, line = map(clean, (title, label, line))
    accent = {"done": GOOD, "failed": BAD, "interrupted": BAD}.get(
        state, accent_for(label)
    )
    if state == "done":
        fraction = 1.0
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    frame(d, HEIGHT, accent)

    width = WIDTH - BAR - 2 * PAD
    x = BAR + PAD
    small, body, big = font("regular", 32), font("regular", 34), font("bold", 54)

    percent = "" if fraction is None else f"{round(100 * clamp(fraction))}%"
    room = width - (d.textlength(percent, font=small) + 30 if percent else 0)
    d.text((x, 50), fit(d, label, small, int(room)), font=small, fill=accent)
    if percent:
        d.text(
            (x + width - d.textlength(percent, font=small), 50),
            percent,
            font=small,
            fill=accent,
        )

    y = 104
    for text in wrap(d, title, big, width, 2):
        d.text((x, y), text, font=big, fill=TITLE)
        y += 66

    top = HEIGHT - 140
    track(image, (x, top, x + width, top + BAR_HEIGHT), fraction, accent)
    if line:
        d.text(
            (x, top + BAR_HEIGHT + 26), fit(d, line, body, width), font=body, fill=MUTED
        )

    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue()


def track(image: Image.Image, box, fraction: float | None, colour) -> None:
    """The bar: filled up to `fraction`, or striped across when that's unknown."""
    d = ImageDraw.Draw(image)
    left, top, right, bottom = box
    radius = (bottom - top) // 2
    d.rounded_rectangle(box, radius, fill=TRACK)
    if fraction is None:
        stripes = Image.new("RGBA", (right - left, bottom - top), (0, 0, 0, 0))
        s = ImageDraw.Draw(stripes)
        h = bottom - top
        for start in range(-h, right - left, STRIPE):
            s.polygon(
                [
                    (start, h),
                    (start + h, 0),
                    (start + h + STRIPE // 2, 0),
                    (start + STRIPE // 2, h),
                ],
                fill=(*colour, 110),
            )
        mask = Image.new("L", stripes.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, right - left - 1, h - 1), radius, fill=255
        )
        # The stripes' own alpha, cut to the bar's rounded shape.
        image.paste(
            stripes, (left, top), ImageChops.multiply(stripes.getchannel("A"), mask)
        )
        return
    filled = left + round((right - left) * clamp(fraction))
    if filled - left >= 2 * radius:
        d.rounded_rectangle((left, top, filled, bottom), radius, fill=colour)
    elif filled > left:
        d.ellipse((left, top, left + 2 * radius, bottom), fill=colour)


def clamp(fraction: float) -> float:
    return min(1.0, max(0.0, float(fraction)))
