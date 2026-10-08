"""The gateway's front for research-runner: deep research runs, for a gateway client.

In AnythingLLM, deep-research is a skill that starts a run. Here research_start does the
same, and research_wait's result holds the whole report, which the runner also saves to the
agent's files; a run from here has no workspace, so no workspace's documents get it. The
models are the runner's defaults (research.job.Request), not the skill's setup args.

A client's runs are its own: each call adds the client as their owner (owner:
"client-<name>", from gateway.grants.client through client_key; never from the
arguments), so research_wait and research_runs reach only the runs it started
(runs.service), and so are their reports.

Declared like a front's tools (a signature and a docstring, no body); gateway.app serves
them as the `research` group, named with PREFIX (research_start, …), while the op each
sends to the runner keeps its own name (start, …). Not an MCP server of its own, so nothing
in the container runs it.

Config (environment):
  RESEARCH_SOCKET  the runner's socket (gateway.app sets it to the host's path)
"""

from typing import Annotated, Any, Literal

import hostrpc
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from gateway.sandbox import client_key

mcp = MCPServer("research")

# The gateway serves each tool here as PREFIX + its name.
PREFIX = "research_"

RunId = Annotated[
    str, Field(description="The run id research_start gave, e.g. 'dr-1a2b3c4d'.")
]

# The runner's socket: $RESEARCH_SOCKET, else storage/everythingllm/research/runner.sock.
FOLDER, ENV = "research", "RESEARCH_SOCKET"
runner = hostrpc.caller(ENV, "research runner", error=ToolError)


async def call(op: str, args: dict[str, Any]) -> Any:
    """Send `op` with the client as owner, never one from the arguments."""
    return await runner(op, {**args, "owner": client_key("Research")})


tool = hostrpc.forwarder(call, mcp.add_tool)


@tool
async def start(
    question: Annotated[
        str,
        Field(
            description="The full research question, self-contained and specific: the "
            "scope, time frame and what to compare, since the researchers see nothing else."
        ),
    ],
    depth: Annotated[
        Literal["quick", "standard", "thorough"] | None,
        Field(
            description='"quick" (a few minutes), "standard" (the default, about 5-10 '
            'minutes) or "thorough" (10-20 minutes, only when asked for exhaustive '
            "research)."
        ),
    ] = None,
    sub_questions: Annotated[
        list[str | dict[str, Any]] | None,
        Field(
            description='Optional: your own split of the question, each a goal ("What do '
            'field studies measure in Norway?") or {"goal": "...", "queries": ["search '
            'terms"]}, at most as many as the depth has workers (quick 3, standard 5, '
            "thorough 8). Without it, the planner splits the question."
        ),
    ] = None,
    title: Annotated[
        str | None,
        Field(
            description="Optional, with sub_questions: the report's title (at most 120 "
            "characters); the question otherwise."
        ),
    ] = None,
) -> dict:
    """Start an in-depth, multi-source web research run on the server, which plans the
    question, researches it with parallel workers and writes a long cited report. It takes
    minutes; use it only for deep research, a report, or a review that needs many
    sources. Answers at once with {run_id, queued (runs it waits for), card (a Markdown
    link to its live progress card)}. Follow it with research_wait; once done, its
    result's reply has the key findings and the whole report, in a <report> tag. Don't
    start the same question twice."""


@tool
async def wait(
    run_id: RunId,
    since: Annotated[
        int, Field(description="How many events you have already seen; 0 at first.")
    ] = 0,
) -> dict:
    """Wait up to 45 seconds for a research run's news: {events (progress lines from
    `since` on), done, result}. Call it again with since increased by len(events) until
    done is true; result is then {status, reply, sources, title, file, error}. Runs are
    kept an hour after they end."""


@tool
async def runs() -> dict:
    """This client's research runs that research-runner holds:
    {runs: [{run_id, question, started, done}]}."""
