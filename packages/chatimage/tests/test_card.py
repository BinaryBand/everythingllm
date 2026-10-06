import io
import re

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
    assert file.stat().st_mode & 0o777 == 0o644
    assert Image.open(file).size == (linkcard.WIDTH, linkcard.HEIGHT)
    assert [p.name for p in file.parent.iterdir()] == [file.name]  # no temp files left


def test_republishing_replaces_the_card_and_changes_its_version(tmp_path):
    url = "https://h/news/daily/x/"
    one = linkcard.make(tmp_path, url, "One", "News")
    two = linkcard.make(tmp_path, url, "Two", "News")
    assert one.split("](")[1].split("?v=")[0] == two.split("](")[1].split("?v=")[0]
    assert one.split("?v=")[1] != two.split("?v=")[1]
    assert len(list((tmp_path / "_cards").iterdir())) == 1
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
    assert len(drew) == 1
