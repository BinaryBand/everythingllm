"""The live cards (browser.live): a tab's screenshot under a strip, pushed as it changes;
the card's link goes to the take-over view while the browser runs."""

import asyncio
import io

from browser import live
from browser.runner import Runner
from browser_fakes import Clock, FakePodman, config, jpeg, scope
from chatimage import THEMES
from PIL import Image


def test_a_frame_is_the_strip_over_the_screenshot_dimmed_once_closed():
    shot = jpeg((250, 250, 250), (640, 400))  # scaled to the card's width
    for state in live.STATES:
        image = Image.open(
            io.BytesIO(
                live.picture(shot, "career", state, "T", "https://x/", "Clicked e1")
            )
        )
        assert image.format == "JPEG" and image.size == (live.WIDTH, live.STRIP + 800)
        middle = image.convert("RGB").getpixel((live.WIDTH // 2, live.STRIP + 400))
        assert isinstance(middle, tuple)
        assert (middle[0] > 200) == (state != "closed")
    blank = Image.open(io.BytesIO(live.picture(b"", "career", "working", "", "", "")))
    assert blank.size == (live.WIDTH, live.STRIP + 360)

    def near(a, b):  # a JPEG's colours are only close
        return all(abs(x - y) <= 6 for x, y in zip(a, b, strict=True))

    for theme, p in THEMES.items():
        themed = Image.open(
            io.BytesIO(live.picture(b"", "career", "user", "", "", "", theme))
        ).convert("RGB")
        assert near(themed.getpixel((live.WIDTH // 2, live.STRIP + 20)), p.panel)
        assert near(themed.getpixel((4, 20)), p.user)  # the strip's stripe
    assert (
        Image.open(
            io.BytesIO(live.picture(b"not a jpeg", "w", "idle", "", "", ""))
        ).size[0]
        == live.WIDTH
    )


async def get(port, path, method="GET"):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"{method} {path} HTTP/1.1\r\nHost: h\r\n\r\n".encode())
    await writer.drain()
    return reader, writer


async def first_frame(reader):
    head = await reader.readuntil(b"\r\n\r\n")
    part = await reader.readuntil(b"\r\n\r\n")
    length = int(part.split(b"Content-Length: ")[1].split(b"\r\n")[0])
    return head, part, await reader.readexactly(length)


def test_the_card_streams_the_tab_and_links_to_the_take_over_view(tmp_path):
    async def main():
        podman = FakePodman()
        r = Runner(config(tmp_path), podman=podman, now=Clock())
        server = await live.Live(r).serve(0)
        port = server.sockets[0].getsockname()[1]
        try:
            await r.op_open(scope(), "example.com")
            tab = r.threads[("career", "7")]
            for path in (
                f"/_live/browser/{tab.id}.jpg",
                f"/{tab.id}.jpg",
            ):  # a route that strips the prefix
                reader, writer = await get(port, path)
                head, part, frame = await first_frame(reader)
                assert b"multipart/x-mixed-replace" in head and b"image/jpeg" in part
                assert Image.open(io.BytesIO(frame)).format == "JPEG"
                assert tab.viewers == 1  # watching counts, which keeps the browser up
                writer.close()
                await asyncio.sleep(1.2)
                assert tab.viewers == 0
            reader, writer = await get(port, f"/_live/browser/{tab.id}")
            head = await reader.read()
            s = r.sessions["career"]
            assert (
                b"302 Found" in head
                and f"Location: {r.takeover(s, tab)}".encode() in head
            )
            await r.stop("career")
            reader, _ = await get(port, f"/_live/browser/{tab.id}")
            head, _, body = (await reader.read()).partition(b"\r\n\r\n")
            assert (
                b"200 OK" in head
                and f"Content-Security-Policy: {live.CSP}".encode() in head
            )
            assert b"tab is closed" in body
            reader, _ = await get(port, "/_live/browser/bw-0000000000000000.jpg")
            head, _, body = (await reader.read()).partition(b"\r\n\r\n")
            assert b"image/png" in head and Image.open(io.BytesIO(body)).format == "PNG"
            reader, _ = await get(port, "/_live/browser/bw-nothex.jpg")
            assert b"404" in await reader.read()
            reader, _ = await get(port, f"/_live/browser/{tab.id}.jpg", "POST")
            assert b"405" in await reader.read()
        finally:
            server.close()
            await podman.close()

    asyncio.run(main())


def test_a_closed_tab_shows_its_last_look_until_its_opened_again(tmp_path):
    async def main():
        podman = FakePodman()
        r = Runner(config(tmp_path), podman=podman, now=Clock())
        try:
            await r.op_open(scope(), "example.com")
            tab = r.threads[("career", "7")]
            frames = live.Live(r).frames(tab)
            await anext(frames)
            await r.op_close(scope())
            closed = Image.open(io.BytesIO(await anext(frames)))
            assert (
                closed.convert("L").getpixel((live.WIDTH // 2, live.STRIP + 300)) < 120  # ty: ignore[unsupported-operator]
            )  # dimmed
            waiting = asyncio.ensure_future(anext(frames))
            await asyncio.sleep(0.1)
            assert not waiting.done()  # a closed tab waits to be used again
            await r.op_open(scope(), "example.com")
            await asyncio.wait_for(waiting, 2)
            await frames.aclose()  # ty: ignore[unresolved-attribute] - a generator's
            assert tab.viewers == 0
        finally:
            await podman.close()

    asyncio.run(main())


def test_a_login_requests_card_shows_how_it_stands_and_links_to_its_form(tmp_path):
    async def main():
        podman = FakePodman()
        r = Runner(config(tmp_path), podman=podman, now=Clock())
        server = await live.Live(r).serve(0)
        port = server.sockets[0].getsockname()[1]
        try:
            await r.op_open(scope(), "https://linkedin.com/login")
            asked = await r.op_ask_login(scope())
            req = r.asked[asked["request"]]
            for path in (f"/_live/browser/login/{req.id}.png", f"/login/{req.id}.png"):
                reader, writer = await get(port, path)
                head, part, frame = await first_frame(reader)
                assert b"multipart/x-mixed-replace" in head and b"image/png" in part
                assert Image.open(io.BytesIO(frame)).format == "PNG"
                writer.close()
            # The stream sends the answer, then ends.
            reader, writer = await get(port, f"/_live/browser/login/{req.id}.png")
            await first_frame(reader)
            await r.fulfil(req, "linkedin.com", "alice", "pw", "", False)
            rest = await asyncio.wait_for(reader.read(), 5)
            assert rest.count(b"Content-Type: image/png") == 1 and rest.endswith(
                b"--\r\n"
            )
            reader, _ = await get(port, f"/_live/browser/login/{req.id}")
            head = await reader.read()
            assert b"302 Found" in head
            assert f"Location: {r.login_form(req)}".encode() in head
            assert (
                r.login_form(req) == f"https://host.example.ts.net:8454/login/{req.id}/"
            )
            nobody = "lr-" + "0" * 32
            reader, _ = await get(port, f"/_live/browser/login/{nobody}.png")
            head, _, body = (await reader.read()).partition(b"\r\n\r\n")
            assert b"image/png" in head and Image.open(io.BytesIO(body)).format == "PNG"
            reader, _ = await get(port, f"/_live/browser/login/{nobody}")
            head, _, body = (await reader.read()).partition(b"\r\n\r\n")
            assert b"200 OK" in head and b"isn&#x27;t known here" in body
            reader, _ = await get(port, "/_live/browser/login/lr-short.png")
            assert b"404" in await reader.read()
        finally:
            server.close()
            await podman.close()

    asyncio.run(main())


def test_every_state_of_a_login_request_has_a_card():
    from browser.runner import LoginRequest

    req = LoginRequest("lr-x", "career", "7", "bw-x", ["linkedin.com"], "https://x/", 0)
    for state in live.ASKED:
        image = Image.open(io.BytesIO(live.asked_picture(req, state)))
        assert image.format == "PNG"
        light = Image.open(io.BytesIO(live.asked_picture(req, state, "light")))
        assert light.convert("RGB").getpixel((800, 20)) == THEMES["light"].panel
