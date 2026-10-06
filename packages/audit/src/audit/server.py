"""MCP server with health checks over the AnythingLLM setup, for the System Audit job.

A front for audit-runner on the host (audit/tools.py), which runs the checks: each tool
call goes to it over a Unix socket in storage, and the text it sends back is the tool's
result. On the host the checks can read the journal, every service's socket and the sites.
Publishing the report and running a job are skills (publish-report, run-job;
audit.tools.SKILLS), not tools here.

Config (environment):
  AUDIT_SOCKET  the runner's socket (default storage/everythingllm/audit/runner.sock, as the container sees it)
"""

from typing import Annotated

import hostrpc
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from audit.services import WATCHED

mcp = MCPServer("audit")

SinceHours = Annotated[
    int, Field(description="How far back to look, in hours.", ge=1, le=24 * 31)
]
JobName = Annotated[
    str, Field(description="Scheduled job name, e.g. 'Daily News Page'.")
]


tool = hostrpc.forwarder(
    hostrpc.caller("audit", "AUDIT_SOCKET", "audit runner", error=ToolError),
    mcp.add_tool,
)


@tool
async def run_checks(since_hours: SinceHours = 24) -> str:
    """Run every health check (service logs, web search, code sandbox, host services,
    OpenRouter credit, scheduled jobs, deep-research runs, published sites) and return the numbered findings
    grouped by severity: fail, warn, info. These are what publish_report publishes."""


@tool
async def journal_lines(
    service: Annotated[str, Field(description=f"One of: {', '.join(WATCHED)}.")],
    since_hours: SinceHours = 24,
    contains: Annotated[
        str, Field(description="Only lines containing this text (case-insensitive).")
    ] = "",
) -> str:
    """Raw log lines from one service, newest last, to look closer at a log finding."""


@tool
async def job_run(
    job: JobName,
    run_id: Annotated[
        int, Field(description="A run id from run_checks; 0 for the job's latest run.")
    ] = 0,
) -> str:
    """One scheduled-job run in detail: status, error, tool calls with their results,
    progress lines and the final reply."""


@tool
async def research_run(
    question: Annotated[
        str,
        Field(
            description="Words from the question the run was given, e.g. 'bitcoin'; leave empty for the newest run."
        ),
    ] = "",
    index: Annotated[
        int,
        Field(
            description="Among the runs that match, 0 for the newest, 1 for the one before, ...",
            ge=0,
        ),
    ] = 0,
    since_hours: SinceHours = 24 * 7,
) -> str:
    """One deep-research run from its run log: question, outcome, stats and its last progress lines.
    Includes runs still going (status "running") and ones cut short without a result (status
    "interrupted", usually by a restart of the server). Look a run up by its `question`; with
    none matching, the recent runs are listed instead."""
