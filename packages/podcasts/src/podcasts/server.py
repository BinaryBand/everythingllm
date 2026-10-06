"""MCP server: subscribe to podcasts, and download their episodes into a private feed.

A front for podcasts-runner on the host (podcasts/tools.py), which does the work: each
tool call goes to it over a Unix socket in storage, and the text it sends back is the
tool's result. Nothing here touches the feeds or the audio. Adding and removing a podcast
are skills (add-podcast, remove-podcast; podcasts.tools.SKILLS), not tools here.

Config (environment):
  PODCASTS_SOCKET  the runner's socket (default storage/everythingllm/podcasts/runner.sock, as the container sees it)
"""

from typing import Annotated

import hostrpc
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

mcp = MCPServer("podcasts")


tool = hostrpc.forwarder(
    hostrpc.caller("podcasts", "PODCASTS_SOCKET", "podcasts runner", error=ToolError),
    mcp.add_tool,
)


@tool
async def find_podcast(
    query: Annotated[
        str,
        Field(
            description="The show's name, or a link to it: Apple Podcasts, the show's website, or a feed URL."
        ),
    ],
) -> str:
    """Find a podcast (show) by name to add to the podcast list: returns its RSS feed for add_podcast. Searches Apple's podcast directory by name
    (or reads an Apple Podcasts link or the show's web page), then checks every feed loads.
    Use this before web search; confirm the right show with the user if several match."""


@tool
async def list_podcasts() -> str:
    """List the podcasts on the podcast list (subscribed shows) with their private feed URLs, downloaded episodes
    and how much ad time was cut from each, what the sync is working on now, and errors from the last sync."""


@tool
async def search_podcasts(
    query: Annotated[
        str,
        Field(
            description="Words or a phrase said in the episode, e.g. 'Bigfoot' or 'Tylenol murders'."
        ),
    ],
    slug: Annotated[
        str, Field(description="Only this podcast; leave empty for all.")
    ] = "",
) -> str:
    """Search what was said in the downloaded podcast episodes (their transcripts): returns the show, episode and
    time of each mention with the words around it. Episodes are transcribed in the background after they download,
    so the newest may not be searchable yet."""


@tool
async def refresh_podcasts(
    slug: Annotated[
        str, Field(description="Only this podcast; leave empty for all.")
    ] = "",
) -> str:
    """Check feeds for new episodes, download them and delete ones beyond `keep`.
    Returns at once; the work happens in the background."""
