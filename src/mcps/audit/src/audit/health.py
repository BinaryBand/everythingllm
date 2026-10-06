"""`python -m audit.health`: the checks behind `make health` (scripts/health.sh).

units      print the host units to check, one per line (from services.WATCHED)
sockets    on the host: every host service the container talks to answers `ping` on its
           socket in storage, the sandbox runner with no problems. Prints OK/FAIL lines and exits 1 if anything failed.
[config]   inside the AnythingLLM container: every MCP server in the live config (or
           `config`) starts and lists its tools. Prints
           OK/FAIL lines and exits 1 if anything failed.
"""

import asyncio
import json
import os
import sys

import hostrpc
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import PaginatedRequestParams

from audit.checks import ping_all
from audit.services import RUNNERS, WATCHED

MCP_CONFIG = f"{hostrpc.CONTAINER_STORAGE}/plugins/anythingllm_mcp_servers.json"
START_SECONDS = 60  # per server; they all start at once
PING_SECONDS = 5  # a host service that's up answers at once


def units() -> list[str]:
    """Our host units: user units by name, Quadlet containers (systemd-<name>) as <name>.service."""
    return [
        w if w.endswith(".service") else f"{w.removeprefix('systemd-')}.service"
        for w in WATCHED
    ]


async def tool_count(cfg: dict) -> int:
    """Start one MCP server as AnythingLLM would and count its tools."""
    params = StdioServerParameters(
        command=cfg["command"],
        args=cfg.get("args", []),
        env={**os.environ, **cfg.get("env", {})},
    )
    with open(os.devnull, "w") as quiet:  # noqa: ASYNC230 - devnull never blocks
        async with (
            stdio_client(params, errlog=quiet) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            count, cursor = 0, None
            while True:
                page = await session.list_tools(
                    params=PaginatedRequestParams(cursor=cursor) if cursor else None
                )
                count += len(page.tools)
                if not (cursor := page.next_cursor):
                    return count


async def mcp_servers(config: str) -> list[tuple[bool, str]]:
    # A small local file, read before any server starts.
    with open(config) as f:  # noqa: ASYNC230
        servers = json.load(f)["mcpServers"]
    counts = await asyncio.gather(
        *(asyncio.wait_for(tool_count(cfg), START_SECONDS) for cfg in servers.values()),
        return_exceptions=True,
    )
    results = []
    for name, n in zip(servers, counts):
        if isinstance(n, int):
            results.append((True, f"MCP {name} ({n} tools)"))
        else:
            why = (
                f"no answer in {START_SECONDS} s"
                if isinstance(n, TimeoutError)
                else f"{type(n).__name__}: {n}"
            )
            results.append((False, f"MCP {name}: {why}"))
    return results


def runners() -> list[tuple[bool, str]]:
    # The audit's own ping, so both say the same thing about the host services.
    down = ping_all(hostrpc.storage(), PING_SECONDS)
    return [
        (name not in down, f"{name}: {down[name]}" if name in down else name)
        for name in RUNNERS
    ]


def report(results: list[tuple[bool, str]]) -> None:
    for good, text in results:
        print(f"  {'OK  ' if good else 'FAIL'}  {text}")
    sys.exit(0 if all(good for good, _ in results) else 1)


def main() -> None:
    args = sys.argv[1:]
    if args == ["units"]:
        print("\n".join(units()))
        return
    if args == ["sockets"]:
        report(runners())
    report(asyncio.run(mcp_servers(args[0] if args else MCP_CONFIG)))


if __name__ == "__main__":
    main()
