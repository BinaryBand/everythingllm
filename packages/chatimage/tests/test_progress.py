import io

import pytest
from chatimage import progress
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
            image.getpixel((x, y)) != progress.TRACK for x in range(progress.WIDTH)
        )

    assert filled(0.2) < filled(0.5) < filled(0.9)
