import asyncio
import json
import threading

import pytest
from research import job, runner
from research.runlog import RunLog


async def call(socket, op, **args):
    reader, writer = await asyncio.open_unix_connection(str(socket))
    writer.write(json.dumps({"op": op, "args": args}).encode() + b"\n")
    await writer.drain()
    reply = json.loads(await reader.readline())
    writer.close()
    return reply


class Gate:
    """A fake job.run: says something, then waits until let through."""

    def __init__(self):
        self.go = threading.Event()
        self.running = 0
        self.peak = 0
        self.lock = threading.Lock()
        self.closed = []
        self.reqs = []

    def __call__(self, req, settings, progress, chat_closed, meter):
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        self.reqs.append(req)
        progress(f"researching {req.question}")
        meter(0.5)
        self.go.wait(5)
        self.closed.append(chat_closed())
        with self.lock:
            self.running -= 1
        return {
            "status": "ok",
            "reply": f"done: {req.question}",
            "sources": [{"url": "https://a/", "title": "A"}],
            "url": f"https://h:8445/research/reports/{req.question}/",
            "title": f"Report on {req.question}",
        }


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "WAIT", 0.5)
    settings = job.Settings(
        storage=tmp_path,
        searxng_url="",
        api="",
        env_file="",
        runlogs=tmp_path / "logs" / "deep-research",
        pages_url="https://h:8445/",
        live_port=0,  # any free port; the runner's live server says which
    )
    gate = Gate()
    socket = tmp_path / "research" / "runner.sock"

    async def start():
        task = asyncio.create_task(
            runner.serve(settings, socket, runner.Runner(settings, execute=gate))
        )
        for _ in range(100):
            if socket.exists():
                break
            await asyncio.sleep(0.01)
        return task

    return settings, gate, socket, start


def test_a_run_starts_reports_progress_and_finishes(served):
    _settings, gate, socket, start = served

    async def go():
        server = await start()
        assert socket.stat().st_mode & 0o777 == 0o660
        started = await call(
            socket, "start", question="  Bitcoin?  ", depth="quick", workspace="career"
        )
        assert started["ok"] and started["result"]["queued"] == 0
        run_id = started["result"]["run_id"]
        first = (await call(socket, "wait", run_id=run_id))["result"]
        assert first == {
            "events": ["researching Bitcoin?"],
            "done": False,
            "result": None,
        }
        # Nothing new: the wait times out with no events.
        assert (await call(socket, "wait", run_id=run_id, since=1))["result"][
            "events"
        ] == []
        gate.go.set()
        last = (await call(socket, "wait", run_id=run_id, since=1))["result"]
        while not last["done"]:
            last = (await call(socket, "wait", run_id=run_id, since=1))["result"]
        assert last["result"]["reply"] == "done: Bitcoin?"
        runs = (await call(socket, "runs"))["result"]["runs"]
        assert [(r["run_id"], r["done"]) for r in runs] == [(run_id, True)]
        server.cancel()

    asyncio.run(go())
    assert gate.closed == [False], "someone was waiting, so the chat was open"


def test_two_runs_go_at_once_and_a_third_waits_its_turn(served):
    _settings, gate, socket, start = served

    async def go():
        server = await start()
        ids = []
        for q in ("one", "two", "three"):
            reply = await call(socket, "start", question=q)
            ids.append((reply["result"]["run_id"], reply["result"]["queued"]))
        assert [queued for _, queued in ids] == [0, 0, 1]
        third = (await call(socket, "wait", run_id=ids[2][0]))["result"]
        assert third["events"] == [
            "Waiting for one of the 2 research runs going now to finish first."
        ]
        assert gate.running == 2
        gate.go.set()
        for run_id, _ in ids:
            while not (await call(socket, "wait", run_id=run_id, since=99))["result"][
                "done"
            ]:
                pass
        server.cancel()

    asyncio.run(go())
    assert gate.peak == 2


def test_a_run_nobody_waits_on_counts_the_chat_as_closed(served, monkeypatch):
    _settings, gate, socket, start = served
    monkeypatch.setattr(runner, "FOLLOW_GRACE", 0.05)

    async def go():
        server = await start()
        run_id = (await call(socket, "start", question="q"))["result"]["run_id"]
        await asyncio.sleep(0.2)
        gate.go.set()
        # Let the run ask before waiting on it, which would count as the chat being open.
        while not gate.closed:
            await asyncio.sleep(0.01)
        while not (await call(socket, "wait", run_id=run_id))["result"]["done"]:
            pass
        server.cancel()

    asyncio.run(go())
    assert gate.closed == [True]


def test_bad_requests_get_errors(served):
    _settings, _gate, socket, start = served

    async def go():
        server = await start()
        assert (await call(socket, "start", question=" "))[
            "error"
        ] == "No research question was given."
        assert (
            "no research run 'dr-nope'"
            in (await call(socket, "wait", run_id="dr-nope"))["error"]
        )
        assert (await call(socket, "explode"))["error"] == "unknown op 'explode'"
        assert (
            "bad arguments for start"
            in (await call(socket, "start", question="q", colour="red"))["error"]
        )
        server.cancel()

    asyncio.run(go())


def test_starting_logs_what_an_earlier_runner_left_as_interrupted(served):
    settings, _gate, _socket, start = served
    RunLog(settings.runlogs).start({"question": "killed by a restart"})

    async def go():
        server = await start()
        server.cancel()

    asyncio.run(go())
    assert list((settings.runlogs / "running").iterdir()) == []
    [line] = [
        json.loads(x)
        for f in settings.runlogs.glob("*.jsonl")
        for x in f.read_text().splitlines()
    ]
    assert (line["question"], line["status"]) == ("killed by a restart", "interrupted")


def test_a_client_that_leaves_mid_wait_is_no_error(served, caplog):
    _settings, gate, socket, start = served

    async def go():
        server = await start()
        run_id = (await call(socket, "start", question="q"))["result"]["run_id"]
        await call(socket, "wait", run_id=run_id)  # past the first event
        _reader, writer = await asyncio.open_unix_connection(str(socket))
        writer.write(
            json.dumps({"op": "wait", "args": {"run_id": run_id, "since": 1}}).encode()
            + b"\n"
        )
        await writer.drain()
        writer.transport.abort()  # gone, as when AnythingLLM restarts
        await asyncio.sleep(0.05)
        gate.go.set()
        while not (await call(socket, "wait", run_id=run_id, since=1))["result"][
            "done"
        ]:
            pass
        server.cancel()

    with caplog.at_level("INFO", logger="research-runner"):
        asyncio.run(go())
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert "a client left before its answer" in [r.getMessage() for r in caplog.records]
