import asyncio
import io

from PIL import Image
from runs import live
from runs.runlog import append_line
from runs.service import RunService


class Things(RunService):
    ID_PREFIX = "th-"
    NOUN = "thing"


class ThingLive(live.Live):
    PATH = "/_live/things/"
    ID = r"th-[0-9a-f]{8}"
    LABEL = "Thing"


async def get(port, path):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: h\r\n\r\n".encode())
    await writer.drain()
    head, _, body = (await reader.read()).partition(b"\r\n\r\n")
    writer.close()
    return head, body


def test_the_card_line_links_its_frames_and_page():
    assert ThingLive.card_line("", "th-0123abcd", "x") == ""
    assert ThingLive.card_line("https://h:8445/", "th-0123abcd", "a [b]") == (
        "[![Thing: a \\[b\\]](https://h:8445/_live/things/th-0123abcd.png)]"
        "(https://h:8445/_live/things/th-0123abcd)"
    )


def test_a_runs_page_escapes_what_it_said_under_a_strict_csp(tmp_path):
    async def main():
        s = Things()
        go = asyncio.Event()

        async def work(run, progress, meter):
            progress("<script>alert(1)</script> found it")
            await go.wait()
            return {"status": "ok", "url": "https://h/report/"}

        run = s.new_run("<b>q</b>")
        s.launch(run, work)
        server = await ThingLive(s, tmp_path, "https://h:8445/").serve(0)
        port = server.sockets[0].getsockname()[1]
        await asyncio.sleep(0.01)
        for path in (
            f"/_live/things/{run.id}",
            f"/{run.id}",
        ):  # tailscale serve strips the prefix
            head, body = await get(port, path)
            assert (
                b"200 OK" in head
                and f"Content-Security-Policy: {live.CSP}".encode() in head
            )
            assert b"<script>" not in body and b"&lt;script&gt;" in body
            assert b"&lt;b&gt;q&lt;/b&gt;" in body and b'http-equiv="refresh"' in body
        go.set()
        await asyncio.sleep(0.05)
        head, _ = await get(port, f"/{run.id}")
        assert b"302 Found" in head and b"Location: https://h/report/" in head
        assert b"404" in (await get(port, "/th-nothex!"))[0]
        server.close()

    asyncio.run(main())


def test_a_run_from_the_log_gets_one_frame_and_its_page(tmp_path):
    append_line(
        tmp_path,
        "2026-10-06T10:00:00Z",
        {
            "run_id": "th-0123abcd",
            "subject": "old",
            "status": "failed",
            "seconds": 120,
            "error": "it broke",
            "events": [[1, "x"]],
        },
    )

    async def main():
        server = await ThingLive(Things(), tmp_path, "").serve(0)
        port = server.sockets[0].getsockname()[1]
        head, body = await get(port, "/th-0123abcd.png")
        assert b"image/png" in head and Image.open(io.BytesIO(body)).format == "PNG"
        head, body = await get(port, "/th-0123abcd")
        assert b"<h1>old</h1>" in body and b"failed" in body and b"refresh" not in body
        head, body = await get(port, "/th-99999999.png")
        assert Image.open(io.BytesIO(body)).format == "PNG"
        server.close()

    asyncio.run(main())


def test_a_card_that_fails_to_draw_answers_500_instead_of_hanging(tmp_path):
    class Broken(ThingLive):
        def ended_line(self, state, result):
            raise ValueError("bad line")

    append_line(
        tmp_path,
        "2026-10-06T10:00:00Z",
        {"run_id": "th-0123abcd", "subject": "old", "status": "failed"},
    )

    async def main():
        server = await Broken(Things(), tmp_path, "").serve(0)
        port = server.sockets[0].getsockname()[1]
        head, body = await asyncio.wait_for(get(port, "/th-0123abcd.png"), 5)
        assert (
            b"500 Internal Server Error" in head and body == b"Something went wrong.\n"
        )
        server.close()

    asyncio.run(main())
