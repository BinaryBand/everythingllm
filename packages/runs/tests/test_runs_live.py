import asyncio
import io
import json

from chatimage import THEMES
from PIL import Image
from runs import live
from runs.runlog import append_line
from runs.service import RunService


class Things(RunService):
    ID_PREFIX = "th-"
    NOUN = "thing"


class ThingLive(live.Live):
    PATH = "/_live/things/"
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
        ):  # a route that strips the prefix
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
        for query, theme in (("", "dark"), ("?theme=light", "light")):
            head, body = await get(port, f"/th-0123abcd.png{query}")
            image = Image.open(io.BytesIO(body)).convert("RGB")
            assert image.getpixel((800, 20)) == THEMES[theme].panel
        head, body = await get(port, "/th-0123abcd")
        assert b"<h1>old</h1>" in body and b"failed" in body and b"refresh" not in body
        head, body = await get(port, "/th-99999999.png")
        assert Image.open(io.BytesIO(body)).format == "PNG"
        server.close()

    asyncio.run(main())


def test_a_runs_json_says_how_it_goes_then_how_it_ended(tmp_path):
    async def main():
        s = Things()
        go = asyncio.Event()

        async def work(run, progress, meter):
            progress("searching")
            meter(0.5)
            await go.wait()
            return {"status": "ok", "title": "Found", "url": "https://h/report/"}

        run = s.new_run("what is it")
        s.launch(run, work)
        server = await ThingLive(s, tmp_path, "https://h:8445/").serve(0)
        port = server.sockets[0].getsockname()[1]
        await asyncio.sleep(0.01)
        run.last_seen = 0
        for path in (f"/_live/things/{run.id}.json", f"/{run.id}.json"):
            head, body = await get(port, path)
            assert b"application/json" in head
            assert b"Access-Control-Allow-Origin: *" in head
            assert b"multipart" not in head  # one answer, not a push stream
            got = json.loads(body)
            assert got == {
                "id": run.id,
                "kind": "Thing",
                "subject": "what is it",
                "title": "what is it",
                "state": "running",
                "fraction": 0.5,
                "minutes": 1,
                "started": run.started,
                "steps": ["searching"],
                "line": "searching",
                "url": None,
                "error": None,
            }
        assert run.last_seen > 0  # polling it counts as following it
        go.set()
        await asyncio.sleep(0.05)
        got = json.loads((await get(port, f"/{run.id}.json"))[1])
        assert (got["state"], got["fraction"], got["title"]) == ("done", 1.0, "Found")
        assert (got["line"], got["url"]) == ("Finished", "https://h/report/")
        server.close()

    asyncio.run(main())


def test_a_runs_json_from_the_log_or_unknown(tmp_path):
    append_line(
        tmp_path,
        "2026-10-06T10:00:00Z",
        {
            "run_id": "th-0123abcd",
            "subject": "old",
            "started": "2026-10-06T10:00:00Z",
            "status": "failed",
            "seconds": 180,
            "error": "it broke",
            "events": [[1, "x"], [2, "y"]],
        },
    )

    async def main():
        server = await ThingLive(Things(), tmp_path, "").serve(0)
        port = server.sockets[0].getsockname()[1]
        got = json.loads((await get(port, "/th-0123abcd.json"))[1])
        assert got == {
            "id": "th-0123abcd",
            "kind": "Thing",
            "subject": "old",
            "title": "old",
            "state": "failed",
            "fraction": None,
            "minutes": 3,
            "started": "2026-10-06T10:00:00Z",
            "steps": ["x", "y"],
            "line": "it broke",
            "url": None,
            "error": "it broke",
        }
        got = json.loads((await get(port, "/th-99999999.json"))[1])
        assert (got["state"], got["steps"], got["url"]) == ("unknown", [], None)
        assert got["line"] == ThingLive.unknown_line(None)
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


def test_cards_listen_on_loopback_unless_told_otherwise(tmp_path, monkeypatch):
    async def bound():
        server = await ThingLive(Things(), tmp_path, "").serve(0)
        host = server.sockets[0].getsockname()[0]
        server.close()
        await server.wait_closed()
        return host

    monkeypatch.delenv("LIVE_HOST", raising=False)
    assert asyncio.run(bound()) == "127.0.0.1"
    monkeypatch.setenv("LIVE_HOST", "0.0.0.0")  # in a container
    assert asyncio.run(bound()) == "0.0.0.0"


class Seen:
    """A connection's writer as the card server sees one from `peer` to `local`."""

    def __init__(self, writer, peer, local):
        self.writer, self.names = writer, {"peername": peer, "sockname": local}

    def get_extra_info(self, name, default=None):
        return self.names.get(name) or self.writer.get_extra_info(name, default)

    def __getattr__(self, name):
        return getattr(self.writer, name)


def test_cards_answer_only_loopback_and_their_own_address(tmp_path):
    cards = ThingLive(Things(), tmp_path, "")
    own = ("10.89.79.11", 8450)  # research-runner's container

    async def status(peer, local=own):
        async def seen(reader, writer):
            await cards.handle(reader, Seen(writer, peer, local))

        server = await asyncio.start_server(seen, "127.0.0.1", 0)
        head, _ = await get(server.sockets[0].getsockname()[1], "/th-99999999")
        server.close()
        await server.wait_closed()
        return head.split(b"\r\n")[0]

    # Through the published port, from the container's own address; and on the host.
    assert asyncio.run(status(("10.89.79.11", 40000))) == b"HTTP/1.1 200 OK"
    assert asyncio.run(status(("127.0.0.1", 40000), None)) == b"HTTP/1.1 200 OK"
    # Another container on egress-net.
    assert asyncio.run(status(("10.89.79.13", 40000))) == b"HTTP/1.1 403 Forbidden"
