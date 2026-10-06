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
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert not sock.exists()  # removed when the service stops


def test_a_service_that_isnt_there_is_down(sock):
    with pytest.raises(
        RunnerError, match="The podcasts runner isn.t running on the host"
    ):
        asyncio.run(hostrpc.request(sock, "ping", {}, 5, name="podcasts runner"))


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


def test_storage_comes_from_host_env(monkeypatch):
    monkeypatch.setenv("ANYTHINGLLM_STORAGE", "/x/storage")
    assert hostrpc.storage() == Path("/x/storage")
    monkeypatch.delenv("ANYTHINGLLM_STORAGE")
    assert hostrpc.storage() == Path("/srv/anythingllm/storage")


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
    call = hostrpc.caller("picky", "PICKY_SOCKET", "picky service", error=Refused)

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


def test_socket_path_is_the_env_or_storage(monkeypatch):
    monkeypatch.setenv("ANYTHINGLLM_STORAGE", "/data/allm")
    monkeypatch.delenv("PICKY_SOCKET", raising=False)
    assert hostrpc.socket_path("picky", "PICKY_SOCKET") == Path(
        "/data/allm/everythingllm/picky/runner.sock"
    )
    monkeypatch.setenv("PICKY_SOCKET", "/tmp/p.sock")
    assert hostrpc.socket_path("picky", "PICKY_SOCKET") == Path("/tmp/p.sock")


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


def test_env_values_keeps_only_the_names_and_lets_the_environment_win(
    tmp_path, monkeypatch
):
    env = tmp_path / ".env"
    env.write_text("A='one'\n# c\nB = \"two\"\nSECRET=x\nA=last\n")
    monkeypatch.setenv("B", "env")
    monkeypatch.delenv("C", raising=False)
    assert hostrpc.env_values(env, ["A", "B", "C"]) == {"A": "last", "B": "env"}
    assert hostrpc.env_values(env, ["B"], environ=False) == {"B": "two"}
    assert hostrpc.env_values(tmp_path / "missing", ["A"]) == {}


def test_request_sync_works_from_inside_a_running_loop(sock):
    async def go():
        svc = Echo()
        task = await served(svc, sock)
        call = (
            hostrpc.request_sync
        )  # blocking, so off the loop as a worker thread would be
        assert await asyncio.to_thread(call, sock, "echo", {"text": "hi"}, 5) == {
            "text": "hi"
        }
        with pytest.raises(RunnerError, match="^no, thanks$"):
            await asyncio.to_thread(call, sock, "refuse", {}, 5)
        with pytest.raises(RunnerError, match="didn't answer within 0s"):
            await asyncio.to_thread(call, sock, "hang", {}, 0.1)
        svc.slow.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    with pytest.raises(RunnerError, match="The sandbox runner isn.t running"):
        hostrpc.request_sync(sock, "ping", {}, 5, name="sandbox runner")


@pytest.fixture
def anythingllm(monkeypatch):
    """A stand-in for AnythingLLM's /api/request-token: the logins it saw, and its password."""
    import http.server
    import threading

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, body))
            ok = body.get("password") == "s3cret!"
            self.send_response(200 if ok else 401)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"valid": ok, "token": f"jwt{len(seen)}" if ok else None}
                ).encode()
            )

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(hostrpc, "_tokens", {})
    yield f"http://127.0.0.1:{server.server_address[1]}/api", seen
    server.shutdown()


def test_no_password_means_no_login(anythingllm, tmp_path):
    api, seen = anythingllm
    env = tmp_path / ".env"
    env.write_text("AUTH_TOKEN='s3cret!'\n")  # without JWT_SECRET it isn't protected
    assert hostrpc.anythingllm_headers(api, env) == {}
    assert hostrpc.anythingllm_headers(api, tmp_path / "missing") == {}
    assert seen == []


def test_logs_in_once_and_again_when_fresh(anythingllm, tmp_path):
    api, seen = anythingllm
    env = tmp_path / ".env"
    env.write_text("AUTH_TOKEN='s3cret!'\nJWT_SECRET=abc\n")
    assert hostrpc.anythingllm_headers(api, env) == {"Authorization": "Bearer jwt1"}
    assert hostrpc.anythingllm_headers(api, env) == {"Authorization": "Bearer jwt1"}
    assert seen == [("/api/request-token", {"password": "s3cret!"})]
    assert hostrpc.anythingllm_headers(api, env, fresh=True) == {
        "Authorization": "Bearer jwt2"
    }


def test_a_refused_password_says_so_without_showing_it(anythingllm, tmp_path):
    api, _ = anythingllm
    env = tmp_path / ".env"
    env.write_text("AUTH_TOKEN=wrong-one\nJWT_SECRET=abc\n")
    with pytest.raises(RunnerError, match="refused the password") as e:
        hostrpc.anythingllm_headers(api, env)
    assert "wrong-one" not in str(e.value)
