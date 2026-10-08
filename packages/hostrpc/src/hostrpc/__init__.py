"""How AnythingLLM's skills and the MCP gateway talk to the services we run on the host.

A service listens on a Unix socket in storage (hostenv.socket_path), which the container
sees without a Quadlet change. One request per connection, each way a single line of JSON:
  -> {"op": "run", "args": {...}}
  <- {"ok": true, "result": {...}}  or  {"ok": false, "error": "..."}

The services serve with `Service` and `serve`; the gateway's fronts ask with `request`
(most through `caller`). The agent skills speak the same protocol from node, through
anythingllm/agent-skills/_lib/hostrpc.js.

It also holds what a service needs beside it: `atomic_write`, `local_peer`, which a
service's HTTP server asks of each connection, and hostrpc.safefs, for opening files in
folders a container can write without following a symlink it put there. Where things are
on this host, and AnythingLLM's login, are hostenv's.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import ipaddress
import json
import logging
import os
import signal
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from types import FunctionType
from typing import Any

LIMIT = 1 << 20  # longest line either side reads; a service can pass its own
CALL_TIMEOUT = 55  # for an MCP tool's call; AnythingLLM gives up on one after 60 s


class RunnerError(Exception):
    """An error to show the caller: bad arguments, a missing file, the service being down."""


class Unreachable(RunnerError):
    """The service isn't there to answer: nothing listens, it broke off the call (a restart
    or a crash), or it closed the connection without answering."""


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


def local_peer(peer: Sequence[Any] | None, local: Sequence[Any] | None) -> bool:
    """Whether a TCP connection comes from this side of its server's port: from loopback, or
    from the server's own address. `peer` and `local` are the accepted connection's peername
    and sockname (`(host, port, ...)`, None when unknown).

    A host unit's server listens on 127.0.0.1. One in a service container listens on
    0.0.0.0, and podman's published port (on the host's 127.0.0.1, where the machine's HTTPS
    routes and the health checks reach it) delivers every connection from the container's own address.
    Another container on egress-net connects from an address of its own, and is refused: the
    port is meant to be reached only through the host's loopback."""

    def address(name: Sequence[Any] | None):
        try:
            ip = ipaddress.ip_address(str(name[0])) if name else None
        except ValueError:
            return None
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            return ip.ipv4_mapped
        return ip

    ip = address(peer)
    return ip is not None and (ip.is_loopback or ip == address(local))


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
        raise Unreachable(
            f"The {name} isn't running on the host ({type(e).__name__} on {socket})."
        ) from e
    except OSError as e:  # e.g. a socket the caller may not use
        raise RunnerError(f"The {name} can't be reached ({e}).") from e
    try:
        await write_message(writer, {"op": op, "args": args})
        reply = await asyncio.wait_for(read_message(reader), timeout)
    except TimeoutError as e:
        raise RunnerError(f"The {name} didn't answer within {timeout:.0f}s.") from e
    except OSError as e:  # a reset, as it restarts or crashes
        raise Unreachable(f"The {name} broke off the call ({e}).") from e
    except ValueError as e:  # a reply over `limit`, or cut short
        raise RunnerError(f"The {name}'s answer couldn't be read ({e}).") from e
    finally:
        writer.close()
    return _result(reply, name)


def _result(reply: Any, name: str) -> Any:
    """A reply's result, or RunnerError with its error."""
    if reply is None:
        raise Unreachable(f"The {name} closed the connection without answering.")
    if not isinstance(reply, dict):
        raise RunnerError(f"The {name} answered with something other than a reply.")
    if not reply.get("ok"):
        raise RunnerError(reply.get("error") or "unknown error")
    return reply.get("result")


def caller(
    env: str,
    name: str,
    *,
    error: type[Exception] = RunnerError,
    timeout: float = CALL_TIMEOUT,
    limit: int = LIMIT,
):
    """For an MCP server's tools: `async call(op, args)`, which asks the service whose
    socket is $<env> (read at each call; the gateway sets it, gateway.app.host_sockets) and
    raises `error` (the server's ToolError) with RunnerError's text."""

    async def call(op: str, args: dict[str, Any]) -> Any:
        try:
            socket = os.environ.get(env)
            if not socket:
                raise Unreachable(f"The {name}'s socket isn't set (${env}).")
            return await request(socket, op, args, timeout, name=name, limit=limit)
        except RunnerError as e:
            raise error(str(e)) from e

    return call


def forwarder(call: Callable, register: Callable[[Callable], Any]):
    """A decorator for an MCP server's tools: the decorated function's signature and
    docstring describe the tool (register is the server's add_tool), and calling it sends
    op=<its name>, args=<every parameter, defaults applied> through `call` (from caller),
    so a tool is written as a signature with no body. The decorator's `registered` lists
    what it registered, which the gateway serves again over HTTP."""

    def decorate(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def forward(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            return await call(fn.__name__, dict(bound.arguments))

        register(forward)
        decorate.registered.append(forward)
        return forward

    decorate.registered = []
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
            args = msg.get("args") or {}
            # Only the call's own arguments are the caller's mistake: a TypeError from
            # inside the op is the runner's, and logged as one below.
            try:
                inspect.signature(fn).bind(**args)
            except TypeError as e:
                raise RunnerError(f"bad arguments for {op}: {e}") from e
            result = (
                await fn(**args)
                if inspect.iscoroutinefunction(fn)
                else await asyncio.to_thread(fn, **args)
            )
            return {"ok": True, "result": result}
        except (RunnerError, *self.errors) as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            self.log.exception("op %s failed", op)
            return {"ok": False, "error": f"runner error: {type(e).__name__}: {e}"}

    async def serve_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            try:
                msg = await read_message(reader)
            except ValueError:  # a line over the limit (LimitOverrunError), or not JSON
                msg = "unreadable"
            if isinstance(msg, dict):
                await write_message(writer, await self.reply(msg))
            elif msg is not None:  # told, so the caller doesn't just send it again
                await write_message(
                    writer,
                    {
                        "ok": False,
                        "error": "the request was too long, or not a JSON object",
                    },
                )
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
