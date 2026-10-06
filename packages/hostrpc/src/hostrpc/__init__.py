"""How AnythingLLM's MCP servers and skills talk to the services we run on the host.

A service listens on a Unix socket in storage, which the container sees without a Quadlet
change. One request per connection, each way a single line of JSON:
  -> {"op": "run", "args": {...}}
  <- {"ok": true, "result": {...}}  or  {"ok": false, "error": "..."}

The services serve with `Service` and `serve` (most through `run`); the MCP servers ask
with `request` (most through `caller`), and blocking code on the host (a site build in a
worker thread or a command) with `request_sync`. The agent skills speak the same protocol from node,
through anythingllm/agent-skills/_lib/hostrpc.js.

It also holds the two file helpers every service needs, `atomic_write` and `env_values`, and
`anythingllm_headers`, the login for AnythingLLM's internal API.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import json
import logging
import os
import signal
import socket as socketlib
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from types import FunctionType
from typing import Any

LIMIT = 1 << 20  # longest line either side reads; a service can pass its own
# Storage as the AnythingLLM container sees it.
CONTAINER_STORAGE = "/app/server/storage"
# The folder in storage that holds our services' sockets, apart from AnythingLLM's own:
# a service's socket is <storage>/everythingllm/<folder>/runner.sock.
SOCKETS = "everythingllm"
CALL_TIMEOUT = 55  # for an MCP tool's call; AnythingLLM gives up on one after 60 s


class RunnerError(Exception):
    """An error to show the caller: bad arguments, a missing file, the service being down."""


def atomic_write(file: Path, data: bytes | str, mode: int = 0o644) -> None:
    """Replace `file` in one step (a reader sees the old file or the new, never half),
    through a temp file of its own, so two writers never share one. Text is UTF-8; the
    mode defaults to world-readable, as the pages site must read what's served."""
    fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=f".{file.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode() if isinstance(data, str) else data)
        os.chmod(tmp, mode)  # mkstemp makes it 0600
        os.replace(tmp, file)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def env_values(
    file: str | Path, names: Iterable[str], *, environ: bool = True
) -> dict[str, str]:
    """Just these KEY=value settings from an env file (AnythingLLM's .env, host.env), quotes
    dropped, so a service doesn't hold the others. With `environ` a non-empty value in the
    environment wins. An unreadable file reads as empty; names found nowhere are left out."""
    names = set(names)
    try:
        lines = Path(file).read_text().splitlines()
    except OSError:
        lines = []
    found = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip() in names:
            found[key.strip()] = value.strip().strip("'\"")
    if environ:
        found.update({n: os.environ[n] for n in names if os.environ.get(n)})
    return found


_tokens: dict[str, str] = {}  # AnythingLLM's API -> this process's login token


def anythingllm_headers(
    api: str, env_file: str | Path, *, fresh: bool = False
) -> dict[str, str]:
    """The headers for AnythingLLM's internal API (`<api>/...`, not the developer API's
    /v1): none while it has no password, else a Bearer token from logging in with the
    password in its .env (AUTH_TOKEN, set in the UI's Security settings). One login per
    process, since each one is logged; `fresh` logs in again, after a 401. Tools copy this
    as units.anythingllm_headers."""
    env = env_values(env_file, ("AUTH_TOKEN", "JWT_SECRET"), environ=False)
    if not (env.get("AUTH_TOKEN") and env.get("JWT_SECRET")):
        return {}
    if fresh or api not in _tokens:
        req = urllib.request.Request(
            f"{api.rstrip('/')}/request-token",
            json.dumps({"password": env["AUTH_TOKEN"]}).encode(),
            {"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                token = json.load(res).get("token")
        except urllib.error.HTTPError as e:
            raise RunnerError(
                f"AnythingLLM refused the password in {env_file} ({e.code})"
            ) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise RunnerError(f"couldn't log in to AnythingLLM at {api}: {e}") from None
        if not token:
            raise RunnerError(f"AnythingLLM refused the password in {env_file}")
        _tokens[api] = token
    return {"Authorization": f"Bearer {_tokens[api]}"}


def storage() -> Path:
    """AnythingLLM's storage directory as the host sees it (from host.env)."""
    return Path(os.environ.get("ANYTHINGLLM_STORAGE", "/srv/anythingllm/storage"))


def data_dir() -> Path:
    """EverythingLLM's own data on the host: what only host services read or write, kept
    out of AnythingLLM's storage, which the container mounts. By kind:

      venvs/<name>/        the host services' venvs
      pages/public/        the pages site Caddy serves; pages/entries/, the Zola entries
      sandbox/workspaces/  the sandbox's folders, per workspace: threads/, project/, shared/
      sandbox/public/      each sandbox workspace's /public, served as it is on :8447
      podcasts/            the podcasts' state and audio; podcasts/models/, Whisper's
      research/runs/       the deep-research run log and live runs' markers
      agents/runs/         the delegations' run log and live runs' markers
      relay/               the Nilson relay's database
      backups/             what make units and make deploy replaced"""
    return Path("~/.local/share/everythingllm").expanduser()


def site_dir() -> Path:
    """The pages site's folder on the host, which Caddy serves on :8445."""
    return data_dir() / "pages" / "public"


def socket_path(folder: str, env: str) -> Path:
    """A service's socket on the host: $<env>, else <storage>/everythingllm/<folder>/runner.sock."""
    return Path(os.environ.get(env) or storage() / SOCKETS / folder / "runner.sock")


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """The next message, or None if the other side closed without sending one."""
    line = await reader.readline()
    return json.loads(line) if line else None


async def write_message(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    writer.write(json.dumps(message).encode() + b"\n")
    await writer.drain()


async def request(
    socket: str | Path,
    op: str,
    args: dict[str, Any],
    timeout: float,
    *,
    name: str = "runner",
    limit: int = LIMIT,
) -> Any:
    """Send one request and return its result, or raise RunnerError.

    `name` is how errors refer to the service ("the sandbox runner"). Sync callers use
    asyncio.run(request(...)).
    """
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket), limit=limit)
    except (FileNotFoundError, ConnectionRefusedError) as e:
        raise RunnerError(
            f"The {name} isn't running on the host ({type(e).__name__} on {socket})."
        ) from e
    try:
        await write_message(writer, {"op": op, "args": args})
        reply = await asyncio.wait_for(read_message(reader), timeout)
    except TimeoutError as e:
        raise RunnerError(f"The {name} didn't answer within {timeout:.0f}s.") from e
    finally:
        writer.close()
    if reply is None:
        raise RunnerError(f"The {name} closed the connection without answering.")
    if not reply.get("ok"):
        raise RunnerError(reply.get("error") or "unknown error")
    return reply["result"]


def request_sync(
    socket: str | Path,
    op: str,
    args: dict[str, Any],
    timeout: float,
    *,
    name: str = "runner",
    limit: int = LIMIT,
) -> Any:
    """`request` for blocking code, which may run with or without an event loop of its own."""
    try:
        conn = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
        conn.settimeout(timeout)
        conn.connect(str(socket))
    except (FileNotFoundError, ConnectionRefusedError) as e:
        raise RunnerError(
            f"The {name} isn't running on the host ({type(e).__name__} on {socket})."
        ) from e
    try:
        with conn, conn.makefile("rwb") as f:
            f.write(json.dumps({"op": op, "args": args}).encode() + b"\n")
            f.flush()
            line = f.readline(limit + 1)
    except TimeoutError as e:
        raise RunnerError(f"The {name} didn't answer within {timeout:.0f}s.") from e
    if not line:
        raise RunnerError(f"The {name} closed the connection without answering.")
    reply = json.loads(line)
    if not reply.get("ok"):
        raise RunnerError(reply.get("error") or "unknown error")
    return reply["result"]


def caller(
    folder: str,
    env: str,
    name: str,
    *,
    error: type[Exception] = RunnerError,
    timeout: float = CALL_TIMEOUT,
    limit: int = LIMIT,
):
    """For an MCP server in the container: `async call(op, args)`, which asks the service
    whose socket is $<env>, else <CONTAINER_STORAGE>/everythingllm/<folder>/runner.sock, and raises
    `error` (the server's ToolError) with RunnerError's text."""

    async def call(op: str, args: dict[str, Any]) -> Any:
        socket = (
            os.environ.get(env) or f"{CONTAINER_STORAGE}/{SOCKETS}/{folder}/runner.sock"
        )
        try:
            return await request(socket, op, args, timeout, name=name, limit=limit)
        except RunnerError as e:
            raise error(str(e)) from e

    return call


def forwarder(call: Callable, register: Callable[[Callable], Any]):
    """A decorator for an MCP server's tools: the decorated function's signature and
    docstring describe the tool (register is the server's add_tool), and calling it sends
    op=<its name>, args=<every parameter, defaults applied> through `call` (from caller),
    so a tool is written as a signature with no body."""

    def decorate(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def forward(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            return await call(fn.__name__, dict(bound.arguments))

        register(forward)
        return forward

    return decorate


class Service:
    """A service's ops are the functions it's given, by name, and its `op_<name>` methods;
    each takes the request's args as keywords and returns what goes in "result".
    RunnerError's text goes back as the error, as does that of the service's own `errors`.
    An op that isn't a coroutine (blocking work: files, fetches) runs in a thread."""

    log = logging.getLogger("hostrpc")
    ops: Mapping[str, Callable] = {}
    errors: tuple[
        type[Exception], ...
    ] = ()  # more exceptions whose text is the caller's error

    def __init__(
        self,
        ops: Iterable[FunctionType] = (),
        *,
        errors: tuple[type[Exception], ...] | None = None,
        log: logging.Logger | None = None,
    ):
        self.ops = {f.__name__: f for f in ops}
        if errors is not None:
            self.errors = errors
        if log is not None:
            self.log = log

    async def op_ping(self) -> dict[str, Any]:
        return {}

    async def reply(self, msg: dict[str, Any]) -> dict[str, Any]:
        op = msg.get("op", "")
        fn = (
            self.ops.get(op) or getattr(self, f"op_{op}", None)
            if isinstance(op, str)
            else None
        )
        try:
            if fn is None:
                raise RunnerError(f"unknown op '{op}'")
            try:
                args = msg.get("args") or {}
                result = (
                    await fn(**args)
                    if inspect.iscoroutinefunction(fn)
                    else await asyncio.to_thread(fn, **args)
                )
                return {"ok": True, "result": result}
            except TypeError as e:
                raise RunnerError(f"bad arguments for {op}: {e}") from e
        except (RunnerError, *self.errors) as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            self.log.exception("op %s failed", op)
            return {"ok": False, "error": f"runner error: {type(e).__name__}: {e}"}

    async def serve_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            if (msg := await read_message(reader)) is not None:
                await write_message(writer, await self.reply(msg))
        except (ConnectionResetError, BrokenPipeError):
            # The caller went away mid-answer (a chat closed, AnythingLLM restarted).
            self.log.info("a client left before its answer")
        except Exception:
            self.log.exception("bad client connection")
        finally:
            writer.close()


async def serve(
    service: Service,
    socket: Path,
    *,
    limit: int = LIMIT,
    stop: asyncio.Event | None = None,
) -> None:
    """Serve `service` on `socket` (group-writable, for the container) until `stop` is set,
    or without one until SIGTERM, then remove the socket. A process that runs other servers
    too passes `stop` and handles its signals itself."""
    socket.parent.mkdir(parents=True, exist_ok=True)
    socket.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(
        service.serve_client, path=str(socket), limit=limit
    )
    socket.chmod(0o660)
    service.log.info("listening on %s", socket)
    loop = asyncio.get_running_loop()
    on_sigterm = stop is None
    if on_sigterm:
        stop = asyncio.Event()
        loop.add_signal_handler(signal.SIGTERM, stop.set)
    try:
        async with server:
            await stop.wait()
        service.log.info("stopped")
    finally:
        if on_sigterm:
            loop.remove_signal_handler(signal.SIGTERM)
        socket.unlink(missing_ok=True)


def run(service: Service, folder: str, env: str, *, limit: int = LIMIT) -> None:
    """A service's main(): log to the journal and serve on its socket until SIGTERM."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(serve(service, socket_path(folder, env), limit=limit))


@contextlib.asynccontextmanager
async def serving(service: Service, socket: Path, *, limit: int = LIMIT):
    """`service` on `socket` for the length of the block, ready when it starts (for tests)."""
    stop = asyncio.Event()
    task = asyncio.create_task(serve(service, socket, limit=limit, stop=stop))
    while not socket.exists() and not task.done():
        await asyncio.sleep(0.01)
    try:
        yield socket
    finally:
        stop.set()
        await task
