import io
import re

import chatimage
from chatimage import card as linkcard
from PIL import Image


def test_a_card_is_saved_and_linked(tmp_path):
    line = linkcard.make(
        tmp_path,
        "https://h:8445/trip-plan/",
        "Trip [plan] 🏔️",
        "Pages · career",
        "Ferries and hikes",
    )
    m = re.fullmatch(
        r"\[!\[Trip \\\[plan\\\] 🏔️\]\(https://h:8445/_cards/(\w+\.png)\?v=(\w+)\)\]\(https://h:8445/trip-plan/\)",
        line,
    )
    assert m
    file = tmp_path / "_cards" / m.group(1)
    light = file.with_suffix(".light.png")
    for f in (file, light):
        assert f.stat().st_mode & 0o777 == 0o644
        assert Image.open(f).size == (linkcard.WIDTH, linkcard.HEIGHT)
    # no temp files left
    assert sorted(p.name for p in file.parent.iterdir()) == sorted(
        [file.name, light.name]
    )


def test_a_card_is_drawn_in_each_theme(tmp_path):
    linkcard.make(tmp_path, "https://h/a/", "A", "Pages · career", "About a")
    for theme, palette in chatimage.THEMES.items():
        image = Image.open(linkcard.card_path(tmp_path, "https://h/a/", theme))
        assert image.convert("RGB").getpixel((linkcard.WIDTH // 2, 20)) == palette.panel
        assert image.getpixel((linkcard.WIDTH // 2, 0))[:3] == palette.line
        assert image.getpixel((0, 0))[3] == 0  # the rounded corners are see-through


def test_republishing_replaces_the_card_and_changes_its_version(tmp_path):
    url = "https://h/news/daily/x/"
    one = linkcard.make(tmp_path, url, "One", "News")
    two = linkcard.make(tmp_path, url, "Two", "News")
    assert one.split("](")[1].split("?v=")[0] == two.split("](")[1].split("?v=")[0]
    assert one.split("?v=")[1] != two.split("?v=")[1]
    assert len(list((tmp_path / "_cards").iterdir())) == len(chatimage.THEMES)
    linkcard.remove(tmp_path, url)
    assert not list((tmp_path / "_cards").iterdir())
    linkcard.remove(tmp_path, url)  # nothing left to remove is fine


def test_urls_that_would_break_the_markdown_are_escaped(tmp_path):
    line = linkcard.make(tmp_path, "https://h/data/my (1).csv", "my (1).csv", "Pages")
    assert line.endswith("](https://h/data/my%20%281%29.csv)")


def test_no_card_is_not_an_error(tmp_path):
    (tmp_path / "_cards").write_text("in the way")
    assert linkcard.make(tmp_path, "https://h/x/", "X", "Pages") == ""


def test_long_text_is_cut_short(tmp_path):
    d = linkcard.ImageDraw.Draw(Image.new("RGB", (10, 10)))
    f = linkcard.font("regular", 36)
    lines = linkcard.wrap(d, "word " * 200 + "x" * 300, f, 600, 2)
    assert len(lines) == 2 and lines[1].endswith("…")
    assert all(d.textlength(line, font=f) <= 600 for line in lines)
    [line] = linkcard.wrap(d, "x" * 300, f, 600, 1)
    assert d.textlength(line, font=f) <= 600
    png = linkcard.draw("T" * 500, "L" * 500, "D " * 500, "h/" + "p" * 500)
    assert Image.open(io.BytesIO(png)).size == (linkcard.WIDTH, linkcard.HEIGHT)


def test_symbols_the_fonts_lack_are_dropped():
    assert linkcard.clean(" Snow 🏔️ day ✨ — ok\n") == "Snow day — ok"


def test_an_unchanged_card_is_not_drawn_again(tmp_path, monkeypatch):
    url = "https://h/news/"
    first = linkcard.make(tmp_path, url, "Daily News", "Site · news", "Headlines")
    drew = []
    monkeypatch.setattr(linkcard, "draw", lambda *a: drew.append(a) or b"")
    assert (
        linkcard.make(tmp_path, url, "Daily News", "Site · news", "Headlines") == first
    )
    assert drew == []
    linkcard.make(tmp_path, url, "Daily News", "Site · news", "New description")
    assert [a[-1] for a in drew] == list(chatimage.THEMES)


def test_a_card_for_a_page_served_elsewhere_lives_on_the_pages_site(tmp_path):
    line = linkcard.make(
        tmp_path,
        "https://h:8447/career/trip-plan/",
        "Trip plan",
        "Pages · career",
        images="https://h:8445/",
    )
    assert re.fullmatch(
        r"\[!\[Trip plan\]\(https://h:8445/_cards/\w+\.png\?v=\w+\)\]\(https://h:8447/career/trip-plan/\)",
        line,
    )


def test_a_card_never_goes_through_a_symlink_a_container_planted(tmp_path):
    """The sites and research containers can write the pages site: _cards or a card made
    a symlink mustn't send the host's write elsewhere."""
    site, outside = tmp_path / "site", tmp_path / "outside"
    site.mkdir()
    outside.mkdir()
    (site / "_cards").symlink_to(outside)
    assert linkcard.make(site, "https://h:8445/a/", "A", "Pages", "") == ""
    assert list(outside.iterdir()) == []
    (site / "_cards").unlink()
    (site / "_cards").mkdir()
    name = linkcard.card_path(site, "https://h:8445/a/").name
    (site / "_cards" / name).symlink_to(outside / "planted")
    assert linkcard.make(site, "https://h:8445/a/", "A", "Pages", "")
    assert list(outside.iterdir()) == []
    assert not (site / "_cards" / name).is_symlink()
