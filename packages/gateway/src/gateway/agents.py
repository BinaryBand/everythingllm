"""The gateway's front for agents-runner: delegations to AnythingLLM's own agents.

AnythingLLM's agent delegates through the delegate skill, which refuses a delegated task
(an agents-* workspace); a gateway client is named by its token, so it gets the ops as
tools here instead. Each call adds the calling client as the runs' owner
(owner: "client-<name>", from gateway.grants.client, which the middleware sets from the
token; never from the arguments), so a client sees, waits on and cancels only the
delegations it started (runs.service). Declared like a front's tools (a signature and a docstring, no body);
gateway.app serves them as the `agents` group, named with PREFIX (agents_delegate, …), while
the op each sends to the runner keeps its own name (delegate, …). Not an MCP server of its
own, so nothing in the container runs it.

Config (environment):
  AGENTS_SOCKET  the runner's socket (gateway.app sets it to the host's path)
"""

from typing import Annotated, Any

import hostrpc
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from gateway.sandbox import client_key

mcp = MCPServer("agents")

# The gateway serves each tool here as PREFIX + its name.
PREFIX = "agents_"

RunId = Annotated[
    str, Field(description="The run id agents_delegate gave, e.g. 'dg-1a2b3c4d'.")
]

# The runner's own line limit (agents.runner.LIMIT): a finished wait can be 6 MB.
LIMIT = 8 * 1024 * 1024

# The runner's socket: $AGENTS_SOCKET, else storage/everythingllm/agents/runner.sock.
FOLDER, ENV = "agents", "AGENTS_SOCKET"
runner = hostrpc.caller(ENV, "agents runner", error=ToolError, limit=LIMIT)


async def call(op: str, args: dict[str, Any]) -> Any:
    """Send `op` with the client as owner, never one from the arguments."""
    return await runner(op, {**args, "owner": client_key("Delegation")})


tool = hostrpc.forwarder(call, mcp.add_tool)


@tool
async def delegate(
    goal: Annotated[
        str,
        Field(description="What the whole piece of work is for; every task sees it."),
    ],
    tasks: Annotated[
        list[dict[str, Any]],
        Field(
            description=(
                '1 to 8 tasks, each {"name": "short-name", "profile": "worker" or '
                '"planner", "instructions": "what to do and what to report back"}. '
                'Optionally "material": text for the task to work on, passed as data, '
                'and "tools": false for a task that only works on what it is given.'
            )
        ),
    ],
    then: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                'Optional: {"profile": "planner", "instructions": "..."}, a last task '
                "that gets every task's reply, e.g. to compare or combine them."
            )
        ),
    ] = None,
) -> dict:
    """Hand work to AnythingLLM's own agents, run in parallel on the server. 'worker' tasks
    search and read the web; 'planner' tasks plan, review and write up. Each task sees only
    the goal and its own instructions, so make them self-contained. The tasks can only read.
    Answers at once with {run_id, queued, card}; follow the run with agents_wait. Tasks
    that read many pages cost real money, and delegation has a daily budget: keep it to
    2-4 tasks."""


@tool
async def wait(
    run_id: RunId,
    since: Annotated[
        int, Field(description="How many events you have already seen; 0 at first.")
    ] = 0,
) -> dict:
    """Wait up to 45 seconds for a delegation's news: {events, done, result}. Call it again
    with since increased by len(events) until done is true; result is then the tasks'
    replies, their status and the cost. Runs are kept an hour after they end."""


@tool
async def runs() -> dict:
    """This client's delegations that agents-runner holds: {runs: [{run_id, goal, started,
    done}]}."""


@tool
async def cancel(run_id: RunId) -> dict:
    """Cancel a delegation: tasks that haven't started won't. A task already running
    finishes (AnythingLLM can't stop it), and its result is thrown away."""
