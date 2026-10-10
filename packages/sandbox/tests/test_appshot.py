"""Pictures of apps' pages for their cards (sandbox.appshot, apps/shoot.py)."""

import asyncio
import functools
import io
import shutil
import subprocess

import hostenv
import pytest
from PIL import Image
from sandbox import appsweb, containers
from sandbox.apps import shoot
from sandbox.apps.card import pictured
from sandbox.appshot import Shots
from sandbox.runner import Runner
from test_runner import A, cfg, make, mounts  # noqa: F401 - cfg is a fixture

SITE = "https://ws.example"


def png(size=(1440, 1080), colour=(200, 40, 40)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "PNG")
    return out.getvalue()


def saves(shot: bytes):
    """A container that saves `shot` as its picture."""
    return lambda m: (m["/out"] / "shot.png").write_bytes(shot)


@pytest.fixture
def r(cfg):  # noqa: F811
    return make(cfg, result=(0, "", "", False))


async def made(r, name="groceries"):
    await r.op_app(A, "create", name, starter="list")
    return r.app_scope("career")


async def take(shots, s, theme="dark", name="groceries", title="Groceries"):
    """`shots`' card of the app as it is now, taken unless it's kept."""
    return await shots.take(shots.key(s, name, theme), s, name, title)


def test_the_container_has_no_network_and_sees_only_public(r, tmp_path):
    async def main():
        s = await made(r)
        args = await asyncio.to_thread(
            containers.prepare_shot, s, "sandbox-shot-x", tmp_path / "run"
        )
        joined = " ".join(args)
        assert args[args.index("--network") + 1] == "none"
        assert "--dns" not in args and "proxy" not in joined
        assert mounts(args) == {
            "/public": s.public,
            "/out": tmp_path / "run" / "out",
            "/sandbox": tmp_path / "run" / "code",
        }
        assert f"{s.public}:/public:ro," in joined
        assert args[args.index("--pids-limit") + 1] == containers.SHOT.pids
        assert "--read-only" in args and "ALL" in args
        assert containers.LABEL in args
        assert args[-2:] == [containers.BROWSER_IMAGE, "/sandbox/shoot.py"]
        assert (tmp_path / "run" / "code" / "shoot.py").read_bytes() == (
            containers.SHOOT.read_bytes()
        )

    asyncio.run(main())


def test_a_request_is_answered_only_from_the_workspace_public(tmp_path):
    public = tmp_path / "public"
    (public / "apps" / "todo").mkdir(parents=True)
    (public / "apps" / "todo" / "index.html").write_text("page")
    (public / "apps" / "todo" / "app.js").write_text("js")
    (tmp_path / "secret").write_text("no")
    (public / "apps" / "todo" / "out").symlink_to(tmp_path / "secret")

    def found(url):
        return shoot.local(url, SITE, "career", public)

    page = public / "apps" / "todo" / "index.html"
    assert found(f"{SITE}/career/apps/todo/") == page.resolve()
    assert found(f"{SITE}/career/apps/todo/index.html?x=1") == page.resolve()
    assert found(f"{SITE}/career/apps/todo/app.js").name == "app.js"
    for elsewhere in (
        f"{SITE}/home/apps/todo/",  # another workspace's
        "https://other.example/career/apps/todo/",
        f"{SITE}/career/apps/todo/../../../secret",
        f"{SITE}/career/apps/todo/%2e%2e/%2e%2e/%2e%2e/secret",
        f"{SITE}/career/apps/todo/out",  # a symlink out of /public
        f"{SITE}/career/apps/todo/nothing.js",
        f"{SITE}/career",
    ):
        assert found(elsewhere) is None, elsewhere
    assert shoot.held(f"{SITE}/_apps/career/todo/sync", SITE)
    assert not shoot.held("https://other.example/_apps/career/todo/sync", SITE)


def test_a_picture_is_taken_once_for_each_page_and_theme(r):
    async def main():
        s = await made(r)
        r.podman.effect = saves(png())
        shots = Shots(r)
        assert shots.kept_card(shots.key(s, "groceries", "dark")) is None
        card = await take(shots, s)
        assert card == pictured("Groceries", png(), "dark")
        assert shots.kept_card(shots.key(s, "groceries", "dark")) == card
        runs = r.podman.runs()
        assert len(runs) == 1
        args = runs[0][0]
        assert args[-4:] == [
            "https://ws.example/career/apps/groceries/",
            "career",
            "dark",
            hostenv.user_zone().key,
        ]
        assert r.podman.calls[-1][0][:2] == ["rm", "-f"]
        assert await take(shots, s) == card
        assert len(r.podman.runs()) == 1  # kept
        await take(shots, s, "light")
        assert len(r.podman.runs()) == 2
        await r.op_app(
            A,
            "do",
            "groceries",
            patch=[{"op": "add", "path": "/items/-", "value": {"text": "Eggs"}}],
        )
        assert shots.kept_card(shots.key(s, "groceries", "dark")) is None  # it changed
        await take(shots, s)
        assert len(r.podman.runs()) == 3
        assert len(shots.kept) == 2  # the newer page's dark picture replaced the older
        assert not list(r.config.scripts.iterdir())  # each container's folder is gone

    asyncio.run(main())


def test_two_viewers_wait_for_one_picture(r):
    async def main():
        s = await made(r)
        r.podman.effect = saves(png())
        r.podman.delay = 0.1
        shots = Shots(r)
        first, second = await asyncio.gather(
            take(shots, s),
            take(shots, s),
        )
        assert first == second is not None
        assert len(r.podman.runs()) == 1

    asyncio.run(main())


@pytest.mark.parametrize(
    "container",
    [
        "none saved",
        "failed",
        "timed out",
        "a symlink",
        "not a png",
        "too big",
    ],
)
def test_a_picture_that_cannot_be_used_is_none_and_not_taken_again(
    r, tmp_path, container
):
    secret = tmp_path / "secret.png"
    secret.write_bytes(png())

    def effect(m):
        out = m["/out"] / "shot.png"
        if container == "a symlink":
            out.symlink_to(secret)
        elif container == "not a png":
            out.write_bytes(b"GIF89a" + b"\0" * 64)
        elif container == "too big":
            out.write_bytes(png(size=(5000, 100)))
        elif container != "none saved":
            out.write_bytes(png())

    async def main():
        s = await made(r)
        r.podman.effect = effect
        if container == "failed":
            r.podman.result = (1, "", "Chromium crashed", False)
        if container == "timed out":
            r.podman.result = (137, "", "", True)
        shots = Shots(r)
        assert await take(shots, s) is None
        assert await take(shots, s) is None
        assert len(r.podman.runs()) == 1

    asyncio.run(main())


def test_an_app_without_a_rendered_page_has_no_picture(r):
    async def main():
        s = await made(r)
        (s.public / "apps" / "groceries" / "index.html").unlink()
        assert await take(Shots(r), s) is None
        assert r.podman.runs() == []

    asyncio.run(main())


def test_the_card_shows_the_drawn_card_then_the_picture(r, monkeypatch):
    monkeypatch.setattr(appsweb, "POLL", 0.05)

    async def main():
        s = await made(r)
        r.podman.effect = saves(png())
        web = appsweb.AppsWeb(r)
        stream = web.frames(s, "groceries", "light")
        drawn = await anext(stream)
        assert drawn[:8] == b"\x89PNG\r\n\x1a\n"
        picture = await asyncio.wait_for(anext(stream), 5)
        assert picture == pictured("Groceries", png(), "light")
        await stream.aclose()
        # Another view of the same app as it is gets the picture at once.
        again = web.frames(s, "groceries", "light")
        assert await anext(again) == picture
        await again.aclose()
        assert len(r.podman.runs()) == 1

    asyncio.run(main())


def test_the_picture_card_is_the_page_under_its_title():
    card = pictured("Pomodoro", png(), "dark")
    image = Image.open(io.BytesIO(card))
    assert image.width == 1600
    assert 1100 < image.height < 1500  # scaled to the card, not cut
    tall = Image.open(io.BytesIO(pictured("Long", png((1440, 4000)), "light")))
    assert tall.height < 1800  # cut to no taller than it is wide
    with pytest.raises(ValueError):
        pictured("x", b"not an image", "dark")


# --- the real thing: the browser image's Chromium, in the container the runner starts ---


@functools.cache
def _missing() -> str:
    if shutil.which("podman") is None:
        return "no podman here"
    found = subprocess.run(
        ["podman", "image", "exists", containers.BROWSER_IMAGE],
        capture_output=True,
        check=False,
    )
    return "" if found.returncode == 0 else f"no {containers.BROWSER_IMAGE} here"


THEMED = """<!doctype html>
<title>Themed</title>
<style>
  body { margin: 0; background: #ffffff; }
  @media (prefers-color-scheme: dark) { body { background: #000000; } }
</style>
<img src="https://example.com/x.png" alt="">
<script src="app.js"></script>
<script>App.start(/*APP*/null);</script>
"""


@pytest.mark.skipif(bool(_missing()), reason=_missing() or "ok")
@pytest.mark.xdist_group("podman")
def test_a_real_picture_of_a_page_in_each_theme(cfg):  # noqa: F811
    real = Runner(cfg)  # podman itself

    async def main():
        await real.op_write(A, "/project/apps/themed/page.html", THEMED)
        await real.op_app(A, "create", "themed")
        s = real.app_scope("career")
        assert s is not None
        shots = Shots(real)
        pictures = {}
        for theme in ("dark", "light"):
            card = await take(shots, s, theme, "themed", "Themed")
            assert card is not None, f"no {theme} picture (see the log)"
            image = Image.open(io.BytesIO(card)).convert("RGB")
            pictures[theme] = image.getpixel((image.width // 2, image.height // 2))
        assert pictures["dark"] == (0, 0, 0)
        assert pictures["light"] == (255, 255, 255)

    asyncio.run(main())
