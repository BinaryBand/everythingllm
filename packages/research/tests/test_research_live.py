import asyncio
import io

import pytest
from PIL import Image
from research import job, live, runner
from runs.runlog import append_line
from test_research_runner import Gate, call


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setattr(live.Live, "GAP", 0.01)
    settings = job.Settings(
        storage=tmp_path,
        searxng_url="",
        env_file="",
        runlogs=tmp_path / "logs" / "deep-research",
        pages_url="https://h:8445/",
        live_port=0,
    )
    gate = Gate()
    socket = tmp_path / "research" / "runner.sock"
    research = runner.Runner(settings, execute=gate)

    async def start():
        task = asyncio.create_task(runner.serve(settings, socket, research))
        for _ in range(100):
            if socket.exists() and research.live:
                break
            await asyncio.sleep(0.01)
        assert research.live is not None, "the live server didn't start"
        return task, research.live.sockets[0].getsockname()[1]

    return settings, gate, socket, start


async def request(port: int, path: str, method: str = "GET"):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"{method} {path} HTTP/1.1\r\nHost: h\r\n\r\n".encode())
    await writer.drain()
    return reader, writer


async def get(port: int, path: str, method: str = "GET") -> tuple[bytes, bytes]:
    reader, writer = await request(port, path, method)
    head, _, body = (await reader.read()).partition(b"\r\n\r\n")
    writer.close()
    return head, body


async def next_frame(reader: asyncio.StreamReader) -> bytes | None:
    """The next PNG of a multipart/x-mixed-replace response; None at its end."""
    boundary = await reader.readline()
    if boundary == b"--frame--\r\n":
        return None
    assert boundary == b"--frame\r\n"
    headers = {}
    while (line := await reader.readline()) != b"\r\n":
        key, _, value = line.decode().strip().partition(": ")
        headers[key] = value
    assert headers["Content-Type"] == "image/png"
    png = await reader.readexactly(int(headers["Content-Length"]))
    assert await reader.readexactly(2) == b"\r\n"
    return png


def test_the_card_is_pushed_until_the_run_ends_then_links_to_the_report(served):
    _settings, gate, socket, start = served

    async def go():
        server, port = await start()
        started = (await call(socket, "start", question="bitcoin"))["result"]
        run_id = started["run_id"]
        assert started["card"] == (
            f"[![Deep research: bitcoin](https://h:8445/_live/research/{run_id}.png)]"
            f"(https://h:8445/_live/research/{run_id})"
        )
        assert gate.reqs[0].run_id == run_id and gate.reqs[0].card == started["card"]

        reader, writer = await request(port, f"/_live/research/{run_id}.png")
        head = await reader.readuntil(b"\r\n\r\n")
        assert b"multipart/x-mixed-replace; boundary=frame" in head
        first = await next_frame(reader)
        assert first is not None
        assert Image.open(io.BytesIO(first)).format == "PNG"
        gate.go.set()
        frames = [first]
        while (png := await next_frame(reader)) is not None:
            frames.append(png)
        writer.close()
        assert len(frames) >= 2 and frames[-1] != first

        head, _ = await get(port, f"/_live/research/{run_id}")
        assert head.startswith(b"HTTP/1.1 302 Found")
        assert b"Location: https://h:8445/research/reports/bitcoin/" in head
        server.cancel()

    asyncio.run(go())
    assert gate.closed == [False], (
        "someone was watching the card, so the run was followed"
    )


def test_a_running_run_links_to_a_page_that_reloads(served):
    _settings, gate, socket, start = served

    async def go():
        server, port = await start()
        run_id = (await call(socket, "start", question="<q>"))["result"]["run_id"]
        while not (await call(socket, "wait", run_id=run_id))["result"]["events"]:
            pass
        head, body = await get(port, f"/_live/research/{run_id}")
        gate.go.set()
        server.cancel()
        return head, body

    head, body = asyncio.run(go())
    assert head.startswith(b"HTTP/1.1 200 OK")
    assert b'http-equiv="refresh"' in body
    assert b"<h1>&lt;q&gt;</h1>" in body and b"<li>researching &lt;q&gt;</li>" in body


def test_a_run_the_runner_no_longer_holds_is_drawn_from_the_run_log(served):
    settings, _gate, _socket, start = served
    append_line(
        settings.runlogs,
        "2026-10-06T10:00:00.000Z",
        {
            "run_id": "dr-0123abcd",
            "question": "old",
            "status": "ok",
            "title": "Old report",
            "url": "https://h:8445/research/reports/old/",
            "seconds": 300,
        },
    )

    async def go():
        server, port = await start()
        found = await get(port, "/_live/research/dr-0123abcd.png")
        # As tailscale serve forwards it, without the prefix.
        link = await get(port, "/dr-0123abcd")
        unknown = await get(port, "/_live/research/dr-ffffffff.png")
        server.cancel()
        return found, link, unknown

    (head, png), (link, _), (unknown_head, unknown_png) = asyncio.run(go())
    assert b"Content-Type: image/png" in head
    assert Image.open(io.BytesIO(png)).size == (1600, 400)
    assert b"Location: https://h:8445/research/reports/old/" in link
    assert b"Content-Type: image/png" in unknown_head and unknown_png


def test_other_requests_are_turned_away(served):
    _settings, _gate, _socket, start = served

    async def go():
        server, port = await start()
        replies = [
            await get(port, "/_live/research/../../etc/passwd"),
            await get(port, "/_live/research/dr-xyz.png"),
            await get(port, "/elsewhere"),
            await get(port, "/_live/research/dr-0123abcd.png/x"),
            await get(port, "/_live/research/dr-0123abcd.png", "POST"),
        ]
        server.cancel()
        return [head.split(b"\r\n")[0] for head, _ in replies]

    assert asyncio.run(go()) == [
        b"HTTP/1.1 404 Not Found",
        b"HTTP/1.1 404 Not Found",
        b"HTTP/1.1 404 Not Found",
        b"HTTP/1.1 404 Not Found",
        b"HTTP/1.1 405 Method Not Allowed",
    ]


def test_no_public_host_means_no_card():
    assert live.Live.card_line("", "dr-0123abcd", "q") == ""
    assert live.Live.card_line("https://h:8445", "dr-0123abcd", "a [b]") == (
        "[![Deep research: a \\[b\\]](https://h:8445/_live/research/dr-0123abcd.png)]"
        "(https://h:8445/_live/research/dr-0123abcd)"
    )
