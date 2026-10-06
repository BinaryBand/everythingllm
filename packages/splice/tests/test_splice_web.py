import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest
from splice import web
from splice.plan import Manifest


@pytest.fixture
def dirs(tmp_path):
    root, manifests, audio = (
        tmp_path / "site",
        tmp_path / "manifests",
        tmp_path / "audio",
    )
    for d in (root / "show", manifests / "show", audio):
        d.mkdir(parents=True)
    (root / "index.html").write_text("<h1>Podcasts</h1>")
    (root / "show" / "feed.xml").write_text("<rss/>")
    (root / "show" / "old.mp3").write_bytes(
        bytes(range(256)) * 4
    )  # not moved to the audio folder yet
    (audio / "abc.mp3").write_bytes(bytes(range(100)))
    m = Manifest(
        "audio/mpeg",
        [
            ["file", "abc.mp3", 0, 10],
            ["bytes", "SEVBRA=="],
            ["file", "abc.mp3", 50, 20],
        ],
    )
    (manifests / "show" / "ep.mp3.json").write_text(m.to_json())
    (tmp_path / "secret").write_text("no")
    return root, manifests, audio


@pytest.fixture
def base(dirs):
    handler = type("H", (web.Handler,), {})
    root, manifests, audio = dirs
    handler.configure(root, manifests, audio, "/podcasts")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


EXPECTED = bytes(range(10)) + b"HEAD" + bytes(range(50, 70))


def test_a_manifest_is_served_put_together(base):
    r = httpx.get(f"{base}/podcasts/show/ep.mp3")
    assert r.status_code == 200 and r.content == EXPECTED
    assert (
        r.headers["content-type"] == "audio/mpeg"
        and r.headers["accept-ranges"] == "bytes"
    )
    assert (
        httpx.get(f"{base}/show/ep.mp3").content == EXPECTED
    )  # with the prefix stripped


@pytest.mark.parametrize(
    "rng, status, start, end",
    [
        ("bytes=0-3", 206, 0, 3),
        ("bytes=8-15", 206, 8, 15),  # across all three parts
        ("bytes=30-", 206, 30, 33),
        ("bytes=-5", 206, 29, 33),
        ("bytes=10-999", 206, 10, 33),
        ("bytes=0-1,4-5", 200, 0, 33),  # several ranges: all of it
        ("bytes=34-", 416, None, None),
    ],
)
def test_ranges(base, rng, status, start, end):
    r = httpx.get(f"{base}/podcasts/show/ep.mp3", headers={"Range": rng})
    assert r.status_code == status
    if status == 416:
        assert (
            r.content == b""
            and r.headers["content-range"] == f"bytes */{len(EXPECTED)}"
        )
        return
    assert r.content == EXPECTED[start : end + 1]
    if status == 206:
        assert r.headers["content-range"] == f"bytes {start}-{end}/{len(EXPECTED)}"


def test_if_range_and_etags(base):
    url = f"{base}/podcasts/show/ep.mp3"
    etag = httpx.head(url).headers["etag"]
    assert (
        httpx.get(url, headers={"Range": "bytes=0-1", "If-Range": etag}).status_code
        == 206
    )
    stale = httpx.get(url, headers={"Range": "bytes=0-1", "If-Range": '"old"'})
    assert (stale.status_code, stale.content) == (200, EXPECTED)
    assert httpx.get(url, headers={"If-None-Match": etag}).status_code == 304


def test_head_sends_no_body(base):
    r = httpx.head(f"{base}/podcasts/show/ep.mp3")
    assert (
        r.status_code == 200
        and r.headers["content-length"] == str(len(EXPECTED))
        and r.content == b""
    )


def test_static_files_like_caddy(base, dirs):
    feed = httpx.get(f"{base}/podcasts/show/feed.xml")
    assert feed.text == "<rss/>" and feed.headers["content-type"].startswith("text/xml")
    assert "script-src 'none'" in feed.headers["content-security-policy"]
    assert feed.headers["x-content-type-options"] == "nosniff"
    assert httpx.get(f"{base}/podcasts/").text == "<h1>Podcasts</h1>"
    r = httpx.get(f"{base}/podcasts")
    assert (r.status_code, r.headers["location"]) == (308, "/podcasts/")
    old = httpx.get(f"{base}/podcasts/show/old.mp3", headers={"Range": "bytes=256-259"})
    assert (old.status_code, old.content) == (206, bytes(range(4)))
    last = feed.headers["last-modified"]
    assert (
        httpx.get(
            f"{base}/podcasts/show/feed.xml", headers={"If-Modified-Since": last}
        ).status_code
        == 304
    )


def test_nothing_outside_the_root(base, dirs):
    root = dirs[0]
    (root / "show" / "link.xml").symlink_to(root.parent / "secret")
    (root / "linked").symlink_to(root.parent)
    for path in [
        "show/link.xml",
        "linked/secret",
        "../secret",
        "%2e%2e/secret",
        "show/.hidden",
        "show",
        "a/b/c",
    ]:
        assert httpx.get(f"{base}/podcasts/{path}").status_code == 404, path
    assert httpx.get(f"{base}/podcasts/show/missing.mp3").status_code == 404


def test_a_manifest_never_reaches_through_a_symlink(base, dirs, tmp_path):
    """The podcasts' containers write the audio and manifests folders; this runs on the
    host, so a link planted there must not serve a host file."""
    root, manifests, audio = dirs
    secret = tmp_path / "secret"
    m = Manifest("audio/mpeg", [["file", "x.mp3", 0, 2]])
    (manifests / "show" / "leak.mp3.json").write_text(m.to_json())
    (audio / "x.mp3").symlink_to(secret)
    assert httpx.get(f"{base}/podcasts/show/leak.mp3").status_code == 404
    # A manifest that's a link, or in a linked folder, isn't read either.
    (manifests / "show" / "linked.mp3.json").symlink_to(
        manifests / "show" / "ep.mp3.json"
    )
    (manifests / "other").symlink_to(manifests / "show")
    assert httpx.get(f"{base}/podcasts/show/linked.mp3").status_code == 404
    assert httpx.get(f"{base}/podcasts/other/ep.mp3").status_code == 404
    # Nor the audio folder swapped for a link to somewhere else.
    real = tmp_path / "real-audio"
    audio.rename(real)
    audio.symlink_to(real)
    assert httpx.get(f"{base}/podcasts/show/ep.mp3").status_code == 404


def test_a_manifest_pointing_at_a_missing_file_is_404(base, dirs):
    (dirs[2] / "abc.mp3").unlink()
    assert httpx.get(f"{base}/podcasts/show/ep.mp3").status_code == 404


def test_health(base):
    assert httpx.get(f"{base}/health").text == "ok"
