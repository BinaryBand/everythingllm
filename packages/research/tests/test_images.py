import io
import os
import socket

import httpx
import pytest
from hostrpc import RunnerError
from PIL import Image
from publicweb import public_client
from publicweb.pages import SearchError
from research import images, job, runner

PAGES = "https://h:8445/"


def picture(fmt="JPEG", size=(200, 100), mode="RGB", colour=(200, 40, 40), **save):
    out = io.BytesIO()
    Image.new(mode, size, colour).save(out, fmt, **save)
    return out.getvalue()


def opened(data):
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


@pytest.fixture
def dns(monkeypatch):
    """Every host is public but those named local.*."""

    def getaddrinfo(host, port, *args, **kwargs):
        address = "127.0.0.1" if host.startswith("local.") else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.delenv("EGRESS_PROXY", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def web(files: dict[str, bytes], seen: list | None = None) -> httpx.Client:
    """An images client whose web is `files`, by URL; anything else is a 404."""

    def handler(request):
        if seen is not None:
            seen.append(str(request.url))
        body = files.get(str(request.url))
        return httpx.Response(200, content=body) if body else httpx.Response(404)

    return public_client(
        images.ImageError,
        transport=httpx.MockTransport(handler),
        headers=images.HEADERS,
    )


def result(n):
    """A result as make_image_search gives it."""
    return {
        "title": f"Panda [{n}] (photo)",
        "page": f"https://site{n}.example/page",
        "thumb": f"https://thumbs.example/{n}.jpg",
        "full": f"https://site{n}.example/{n}.jpg",
    }


def pictures(folder, results=(), files=None, seen=None, pages=PAGES):
    """Pictures over a fake search (its `results`, or the SearchError given) and web."""

    def search(query):
        if isinstance(results, Exception):
            raise results
        return list(results)

    return images.Pictures(folder, pages, search, http=web(files or {}, seen))


def test_a_picture_is_saved_anew_without_what_it_carried():
    exif = Image.Exif()
    exif[0x0112] = 6  # orientation: rotate 90°
    exif[0x010F] = "Camera Maker"
    original = picture(size=(200, 100), exif=exif) + b"PK\x03\x04 a zip glued on"
    data, ext, width, height = images.reencode(original, 480)
    assert ext == "jpg" and (width, height) == (100, 200)  # turned upright
    assert b"Camera Maker" not in data and b"a zip glued on" not in data
    assert not opened(data).getexif()


def test_a_big_picture_is_made_smaller_and_an_animation_keeps_its_first_frame():
    data, ext, width, height = images.reencode(picture(size=(2000, 1000)), 480)
    assert (ext, width, height) == ("jpg", 480, 240)
    frames = [Image.new("RGB", (64, 64), c) for c in ((255, 0, 0), (0, 0, 255))]
    gif = io.BytesIO()
    frames[0].save(gif, "GIF", save_all=True, append_images=frames[1:])
    data, ext, _, _ = images.reencode(gif.getvalue(), 480)
    assert ext == "jpg"
    assert opened(data).getpixel((32, 32))[0] > 200  # red, the first frame


def test_a_see_through_picture_stays_a_png_and_an_opaque_one_becomes_a_jpeg():
    clear = picture("PNG", mode="RGBA", colour=(0, 0, 0, 0))
    assert images.reencode(clear, 480)[1] == "png"
    solid = picture("PNG", mode="RGBA", colour=(0, 0, 0, 255))
    assert images.reencode(solid, 480)[1] == "jpg"


@pytest.mark.parametrize(
    "data, why",
    [
        (b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>', "can read"),
        (b"<html>not a picture</html>", "can read"),
        (picture("BMP"), "we take"),
        (picture(size=(1, 1)), "too small"),
    ],
)
def test_what_isnt_a_picture_we_take_is_refused(data, why):
    with pytest.raises(images.ImageError, match=why):
        images.reencode(data, 480)


def test_a_picture_too_large_to_decode_is_refused_before_decoding(monkeypatch):
    monkeypatch.setattr(images, "MAX_PIXELS", 100 * 100)
    with pytest.raises(images.ImageError, match="too large"):
        images.reencode(picture(size=(200, 100)), 480)


def test_a_search_shows_the_thumbnails_in_order_linked_to_their_pages(tmp_path, dns):
    folder = tmp_path / "_webimages"
    files = {
        "https://thumbs.example/1.jpg": picture(size=(300, 150)),
        # 2's thumbnail is small, so its picture is fetched too, and the larger taken.
        "https://thumbs.example/2.jpg": picture(size=(100, 50)),
        "https://site2.example/2.jpg": picture(size=(800, 400), colour=(2, 0, 0)),
        # 3's thumbnail is missing, so its picture is fetched instead; 4 has nothing.
        "https://site3.example/3.jpg": picture(size=(1200, 600), colour=(3, 0, 0)),
    }
    seen = []
    found = pictures(folder, [result(n) for n in (1, 2, 3, 4)], files, seen).search(
        "red panda", count=4
    )
    shown = found["images"]
    assert [i["page"] for i in shown] == [
        f"https://site{n}.example/page" for n in (1, 2, 3)
    ]
    assert [i["source"] for i in shown] == [
        "site1.example",
        "site2.example",
        "site3.example",
    ]
    assert len(found["skipped"]) == 1 and "site4.example" in found["skipped"][0]
    first = shown[0]
    name = first["url"].removeprefix(PAGES + "_webimages/")
    assert (folder / name).is_file() and "/" not in name
    # The title can't break out of the Markdown.
    assert first["image"] == (
        f"[![Panda \\[1\\] (photo)]({first['url']})](https://site1.example/page)"
    )
    assert [(i["width"], i["height"]) for i in shown] == [
        (300, 150),
        (480, 240),
        (480, 240),
    ]
    assert "https://site1.example/1.jpg" not in seen  # the thumbnail did
    # Only the pictures taken are saved.
    assert sorted(os.listdir(folder)) == sorted(
        i["url"].removeprefix(PAGES + "_webimages/") for i in shown
    )


def test_a_search_stops_at_count_and_says_when_nothing_could_be_fetched(tmp_path, dns):
    files = {
        f"https://thumbs.example/{n}.jpg": picture(size=(300, 150), colour=(n, 0, 0))
        for n in range(9)
    }
    found = pictures(tmp_path, [result(n) for n in range(9)], files).search("q", 2)
    assert len(found["images"]) == 2 and len(os.listdir(tmp_path)) == 2
    with pytest.raises(RunnerError, match="none of the pictures"):
        pictures(tmp_path, [result(1)]).search("q")
    with pytest.raises(RunnerError, match="found nothing"):
        pictures(tmp_path, []).search("q")
    down = SearchError("no results; engines unavailable: bing images (timeout)")
    with pytest.raises(RunnerError, match="engines unavailable: bing images"):
        pictures(tmp_path, down).search("q")


def test_a_picture_by_its_url_links_to_it_and_a_local_host_is_refused(tmp_path, dns):
    url = "https://photos.example/bridge.png"
    shown = pictures(tmp_path, files={url: picture("PNG")}).show(url, "The [bridge]")
    shown = shown["images"][0]
    assert shown["image"] == f"[![The \\[bridge\\]]({shown['url']})]({url})"
    assert shown["url"].endswith(".jpg")
    with pytest.raises(RunnerError, match="private or local"):
        pictures(tmp_path).show("https://local.example/x.png")
    with pytest.raises(RunnerError, match="http or https"):
        pictures(tmp_path).show("file:///etc/passwd")
    with pytest.raises(RunnerError, match="PUBLIC_HOST"):
        pictures(tmp_path, files={url: picture()}, pages="").show(url)


def test_the_op_takes_a_query_or_a_url_and_a_sensible_count(tmp_path):
    settings = job.Settings(
        storage=tmp_path,
        searxng_url="",
        env_file="",
        runlogs=tmp_path / "logs",
        pages_url=PAGES,
    )
    r = runner.Runner(settings)
    for args, why in (
        ({}, "give a query"),
        ({"query": "a", "url": "https://x/y.png"}, "not both"),
        ({"query": "a", "count": 7}, "count must be"),
        ({"query": "a", "count": "4"}, "count must be"),
        ({"query": "a", "count": True}, "count must be"),
    ):
        with pytest.raises(RunnerError, match=why):
            r.op_images(**args)
