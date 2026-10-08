import asyncio
import json
import os
from pathlib import Path

import hostrpc
import pytest
from hostrpc import RunnerError, Service


class Echo(Service):
    def __init__(self):
        self.slow = asyncio.Event()

    async def op_echo(self, text: str) -> dict:
        return {"text": text}

    async def op_refuse(self) -> dict:
        raise RunnerError("no, thanks")

    async def op_crash(self) -> dict:
        raise KeyError("x")

    async def op_hang(self) -> dict:
        await self.slow.wait()
        return {}


@pytest.fixture
def sock():
    path = Path("/tmp") / f"hostrpc-test-{os.getpid()}.sock"  # AF_UNIX paths are short
    yield path
    path.unlink(missing_ok=True)


async def served(service, sock):
    task = asyncio.create_task(hostrpc.serve(service, sock))
    for _ in range(100):
        if sock.exists():
            break
        await asyncio.sleep(0.01)
    return task


async def raw(sock, line: bytes) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(sock))
    writer.write(line)
    await writer.drain()
    reply = json.loads(await reader.readline())
    writer.close()
    return reply


def test_requests_get_results_and_errors(sock):
    async def go():
        task = await served(Echo(), sock)
        assert sock.stat().st_mode & 0o777 == 0o660
        assert await hostrpc.request(sock, "echo", {"text": "hi"}, 5) == {"text": "hi"}
        assert await hostrpc.request(sock, "ping", {}, 5) == {}
        with pytest.raises(RunnerError, match="^no, thanks$"):
            await hostrpc.request(sock, "refuse", {}, 5)
        with pytest.raises(RunnerError, match="unknown op 'nope'"):
            await hostrpc.request(sock, "nope", {}, 5)
        with pytest.raises(RunnerError, match="bad arguments for echo"):
            await hostrpc.request(sock, "echo", {"wrong": 1}, 5)
        with pytest.raises(RunnerError, match="runner error: KeyError"):
            await hostrpc.request(sock, "crash", {}, 5)
        assert (await raw(sock, b'{"op": 5}\n'))["error"] == "unknown op '5'"
        # A request that isn't one is answered, not dropped.
        for line in (b"[1]\n", b"not json\n"):
            assert (await raw(sock, line))["error"].startswith(
                "the request was too long"
            )
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert not sock.exists()  # removed when the service stops


def test_a_service_that_isnt_there_is_down(sock):
    with pytest.raises(
        RunnerError, match="The sandbox runner isn.t running on the host"
    ):
        asyncio.run(hostrpc.request(sock, "ping", {}, 5, name="sandbox runner"))


def test_a_slow_answer_times_out(sock):
    async def go():
        svc = Echo()
        task = await served(svc, sock)
        with pytest.raises(RunnerError, match="The runner didn't answer within 0s"):
            await hostrpc.request(sock, "hang", {}, 0.1)
        svc.slow.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())


def test_a_client_that_leaves_is_no_error(sock, caplog):
    async def go():
        svc = Echo()
        task = await served(svc, sock)
        _, writer = await asyncio.open_unix_connection(str(sock))
        writer.close()  # sent nothing
        _reader, writer = await asyncio.open_unix_connection(str(sock))
        writer.write(b'{"op": "hang"}\n')
        await writer.drain()
        writer.transport.abort()
        await asyncio.sleep(0.05)
        svc.slow.set()
        await asyncio.sleep(0.05)
        assert await hostrpc.request(sock, "ping", {}, 5) == {}
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    with caplog.at_level("INFO", logger="hostrpc"):
        asyncio.run(go())
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


class Picky(hostrpc.Service):
    errors = (ValueError,)

    def op_check(self, n: int) -> int:
        if n < 0:
            raise ValueError("n must be positive")
        return n


class Refused(Exception):
    pass


def test_a_front_calls_through_caller_and_gets_the_services_errors(sock, monkeypatch):
    monkeypatch.setenv("PICKY_SOCKET", str(sock))
    call = hostrpc.caller("PICKY_SOCKET", "picky service", error=Refused)

    async def main():
        async with hostrpc.serving(Picky(), sock):
            ok = await call("check", {"n": 2})
            with pytest.raises(
                Refused, match="n must be positive"
            ):  # an `errors` one, not "runner error"
                await call("check", {"n": -1})
            return ok

    assert asyncio.run(main()) == 2
    assert not sock.exists()  # serving() stops the service and it removes its socket
    with pytest.raises(Refused, match="picky service isn't running"):
        asyncio.run(call("check", {"n": 1}))
    monkeypatch.delenv("PICKY_SOCKET")  # read at each call, never guessed
    with pytest.raises(Refused, match=r"picky service's socket isn't set \(\$PICKY"):
        asyncio.run(call("check", {"n": 1}))


def test_a_forwarder_lists_what_it_registered():
    added = []
    tool = hostrpc.forwarder(lambda op, args: None, added.append)

    @tool
    async def first(a: int) -> str:
        """First."""

    @tool
    async def second() -> str:
        """Second."""

    assert tool.registered == added == [first, second]
    assert [f.__name__ for f in tool.registered] == ["first", "second"]


def test_atomic_write_replaces_whole_and_cleans_up(tmp_path, monkeypatch):
    file = tmp_path / "index.html"
    hostrpc.atomic_write(file, "héllo")
    hostrpc.atomic_write(file, b"bytes")
    assert file.read_bytes() == b"bytes"
    assert file.stat().st_mode & 0o777 == 0o644
    monkeypatch.setattr(
        os, "replace", lambda *a: (_ for _ in ()).throw(OSError("full"))
    )
    with pytest.raises(OSError):
        hostrpc.atomic_write(file, "lost")
    assert [p.name for p in tmp_path.iterdir()] == ["index.html"]


def test_a_peer_is_local_from_loopback_or_the_servers_own_address_only():
    # A host unit: everything comes from loopback.
    assert hostrpc.local_peer(("127.0.0.1", 40000), ("127.0.0.1", 8448))
    assert hostrpc.local_peer(("::1", 40000, 0, 0), ("::1", 8448, 0, 0))
    # A service container: the published port delivers from its own address.
    own = ("10.89.79.12", 8448)
    assert hostrpc.local_peer(("10.89.79.12", 40000), own)
    assert hostrpc.local_peer(("::ffff:10.89.79.12", 40000, 0, 0), own)
    # Another container on egress-net, or anything else, is refused.
    assert not hostrpc.local_peer(("10.89.79.13", 40000), own)
    assert not hostrpc.local_peer(("10.89.79.1", 40000), own)
    assert not hostrpc.local_peer(("100.64.0.5", 40000), own)
    assert not hostrpc.local_peer(None, own)
    assert not hostrpc.local_peer(("10.89.79.12", 40000), None)
    assert not hostrpc.local_peer(("testclient", 50000), ("testserver", 80))


def test_a_type_error_inside_an_op_is_the_runners_not_bad_arguments(caplog):
    async def buggy(n: int) -> int:
        return None + n  # type: ignore[operator]

    service = hostrpc.Service([buggy])
    bad = asyncio.run(service.reply({"op": "buggy", "args": {"m": 1}}))
    assert not bad["ok"] and bad["error"].startswith("bad arguments for buggy: ")
    broken = asyncio.run(service.reply({"op": "buggy", "args": {"n": 1}}))
    assert broken["error"].startswith("runner error: TypeError")
    assert "op buggy failed" in caplog.text  # and logged, as the runner's own bug
