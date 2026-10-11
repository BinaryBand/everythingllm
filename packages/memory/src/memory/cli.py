"""memory: the long-term memory from the host's shell. `serve` is memory-runner itself;
the other commands ask it over its socket, as the long-memory skill does, so the model
loads once, in the runner.

  uv run --package memory memory remember "Prefers trains to flights" [--workspace W]
  uv run --package memory memory recall "how does the user like to travel" [-k 5] [--only facts]
  uv run --package memory memory list [--workspace W]
  uv run --package memory memory forget m12 | --path /project/notes.md --workspace W
  uv run --package memory memory ingest /project/notes --workspace W
  uv run --package memory memory serve

Without --workspace a command sees and saves only global facts (the runner's `_host`);
with one, what that workspace's chats see, and `remember` saves for it unless --global.
`ingest` and `forget --path` take sandbox paths in that workspace (/project, /shared/<W>;
/work is the CLI's own thread, `default`'s). --json prints the runner's whole answer.

Config (environment): MEMORY_SOCKET, as memory-runner's.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import hostenv
from hostrpc import RunnerError, request

from memory import runner

TIMEOUT = 120.0


def parse(argv: list[str] | None) -> argparse.Namespace:
    """The command line; each command's `op_args` gives what its op takes."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-w", "--workspace", help="the workspace (default: global facts only)"
    )
    common.add_argument(
        "--json", action="store_true", help="print the whole answer as JSON"
    )
    parser = argparse.ArgumentParser(
        prog="memory", description=__doc__.split("\n\n")[0]
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help: str, op_args) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, parents=[common], help=help)
        sub.set_defaults(op_args=op_args)
        return sub

    remember = command(
        "remember",
        "keep one fact",
        lambda a: {
            "text": a.text,
            "memory_scope": "global"
            if a.everywhere or not a.workspace
            else "workspace",
        },
    )
    remember.add_argument("text")
    remember.add_argument(
        "--global", dest="everywhere", action="store_true", help="for every workspace"
    )
    recall = command(
        "recall",
        "what's closest to a query",
        lambda a: {"query": a.query, "k": a.k, "only": a.only},
    )
    recall.add_argument("query")
    recall.add_argument("-k", type=int, default=5)
    recall.add_argument("--only", choices=["facts", "files"])
    command("list", "the newest facts and the ingested files", lambda a: {})
    forget = command(
        "forget",
        "delete a fact, or drop a file",
        lambda a: {"id": a.id, "path": a.path},
    )
    forget.add_argument("id", nargs="?", help="the fact's id (m12)")
    forget.add_argument("--path", help="an ingested sandbox file or folder to drop")
    ingest = command("ingest", "keep sandbox files' text", lambda a: {"paths": a.paths})
    ingest.add_argument("paths", nargs="+")
    commands.add_parser("serve", help="run memory-runner")
    args = parser.parse_args(argv)
    if (args.command == "ingest" or getattr(args, "path", None)) and not args.workspace:
        parser.error(
            "name the workspace whose sandbox files these are with --workspace"
        )
    return args


def call(args: argparse.Namespace) -> dict[str, Any]:
    scope = {"workspace": args.workspace or runner.HOST, "thread": "default"}
    socket = hostenv.socket_path("memory", "MEMORY_SOCKET")
    return asyncio.run(
        request(
            socket,
            args.command,
            {"scope": scope, **args.op_args(args)},
            TIMEOUT,
            name="memory runner",
        )
    )


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    if args.command == "serve":
        runner.main()
        return 0
    try:
        result = call(args)
    except RunnerError as e:
        print(f"memory: {e}", file=sys.stderr)
        return 1
    print(
        json.dumps(result, indent=2, ensure_ascii=False)
        if args.json
        else result["text"]
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
