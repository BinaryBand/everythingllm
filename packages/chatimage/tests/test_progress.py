import io

import pytest
from chatimage import THEMES, progress
from PIL import Image


@pytest.mark.parametrize("state", progress.STATES)
@pytest.mark.parametrize("fraction", [None, 0, 0.004, 0.42, 1, 7])
def test_every_state_and_fraction_draws_a_frame(state, fraction):
    png = progress.draw(
        "Why is the sky blue? " * 20,
        "Deep research · running",
        fraction,
        "x " * 300,
        state,
    )
    assert Image.open(io.BytesIO(png)).size == (progress.WIDTH, progress.HEIGHT)


def test_an_unknown_state_is_an_error():
    with pytest.raises(ValueError):
        progress.draw("T", "L", 0.5, state="paused")


def test_the_bar_fills_with_the_fraction():
    def filled(fraction):
        image = Image.open(io.BytesIO(progress.draw("T", "L", fraction))).convert("RGB")
        y = progress.HEIGHT - 140 + progress.BAR_HEIGHT // 2
        return sum(
            image.getpixel((x, y)) != THEMES["dark"].track
            for x in range(progress.WIDTH)
        )

    assert filled(0.2) < filled(0.5) < filled(0.9)


@pytest.mark.parametrize("theme", THEMES)
def test_a_frame_is_drawn_in_its_theme(theme):
    p = THEMES[theme]
    image = Image.open(
        io.BytesIO(progress.draw("T", "L", 0.5, "line", "done", theme))
    ).convert("RGB")
    assert image.getpixel((progress.WIDTH // 2, 20)) == p.panel
    assert image.getpixel((10, progress.HEIGHT // 2)) == p.done  # the stripe


def test_an_unknown_theme_is_an_error():
    with pytest.raises(KeyError):
        progress.draw("T", "L", 0.5, theme="sepia")


def test_the_light_theme_keeps_text_readable():
    def luminance(c):
        def channel(v):
            v /= 255
            return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

        r, g, b = map(channel, c)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    def contrast(a, b):
        hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    for p in THEMES.values():
        for colour in (p.title, p.text, p.faint, p.done, p.failed, p.user, *p.accents):
            assert contrast(colour, p.panel) >= 4.5
