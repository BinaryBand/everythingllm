"""agents-run: start a delegation by hand and follow it, as the delegate skill would.

  agents-run "goal" --task name:profile:"instructions" [--task …] [--then profile:"instructions"]

It asks agents-runner over its socket, prints the live card's address, follows the run's
progress and prints every task's reply at the end. Ctrl-C stops following; the delegation
carries on (agents-run --cancel <run_id> cancels it).

Config (environment): AGENTS_SOCKET, as agents-runner.
"""

import argparse
import asyncio
import re
import sys

import hostrpc
from hostrpc import RunnerError

CALL_SECONDS = 60  # a wait is a long poll of 45 s


def task(text: str) -> dict:
    name, _, rest = text.partition(":")
    profile, _, instructions = rest.partition(":")
    if not (name and profile and instructions):
        raise argparse.ArgumentTypeError("a task is name:profile:instructions")
    return {"name": name, "profile": profile, "instructions": instructions}


def then(text: str) -> dict:
    profile, _, instructions = text.partition(":")
    if not (profile and instructions):
        raise argparse.ArgumentTypeError("then is profile:instructions")
    return {"profile": profile, "instructions": instructions}


async def call(op: str, args: dict) -> dict:
    socket = hostrpc.socket_path("agents", "AGENTS_SOCKET")
    return await hostrpc.request(socket, op, args, CALL_SECONDS, name="agents runner")


def card_url(card: str) -> str:
    """The page a card links to, from its Markdown line."""
    found = re.findall(r"\]\(([^()]+)\)$", card)
    return found[0] if found else ""


async def follow(run_id: str) -> dict:
    since = 0
    while True:
        reply = await call("wait", {"run_id": run_id, "since": since})
        for event in reply["events"]:
            print(f"  {event}", flush=True)
        since += len(reply["events"])
        if reply["done"]:
            return reply["result"]


def report(result: dict) -> None:
    print(f"\n{result['status']} (${result.get('cost', 0):.4f})")
    if result.get("error"):
        print(result["error"])
    for o in [
        *(result.get("tasks") or []),
        *([result["then"]] if result.get("then") else []),
    ]:
        print(f"\n== {o['name']} ({o['profile']}): {o['status']}, {o['seconds']} s")
        print(o.get("text") or o.get("error") or "")


async def run(args: argparse.Namespace) -> int:
    if args.cancel:
        print(await call("cancel", {"run_id": args.cancel}))
        return 0
    if not args.goal or not args.task:
        print("give a goal and at least one --task", file=sys.stderr)
        return 2
    started = await call(
        "delegate", {"goal": args.goal, "tasks": args.task, "then": args.then}
    )
    print(
        f"{started['run_id']}: {card_url(started['card']) or 'no live card (no PUBLIC_HOST)'}"
    )
    if started["queued"]:
        print(f"  waits for {started['queued']} other delegation(s) first")
    report(await follow(started["run_id"]))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agents-run", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("goal", nargs="?", help="what the whole piece of work is for")
    parser.add_argument(
        "--task", type=task, action="append", help="name:profile:instructions"
    )
    parser.add_argument(
        "--then", type=then, help="profile:instructions, run over the results"
    )
    parser.add_argument(
        "--cancel", metavar="RUN_ID", help="cancel a delegation instead"
    )
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(run(args)))
    except RunnerError as e:
        sys.exit(f"agents-run: {e}")
    except KeyboardInterrupt:
        sys.exit("stopped following; the delegation carries on")
