"""MCP server for the Zola sites' entries, and the news feeds' headlines the Daily News is
written from. Free-form pages come from the sandbox's publish skill instead.

A front for sites-runner on the host (sites/tools.py), which does the work: each tool
call goes to it over a Unix socket in storage, and the text it sends back is the tool's
result. Nothing here touches the entries, runs zola or fetches a feed.

Config (environment):
  SITES_SOCKET  the runner's socket (default storage/everythingllm/sites/runner.sock, as the container sees it)
"""

from typing import Annotated, Any

import hostrpc
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

mcp = MCPServer("sites")

Site = Annotated[str, Field(description="Site name from list_sites, e.g. 'news'.")]
Section = Annotated[
    str, Field(description="Section name from list_sites, e.g. 'editions'.")
]
Slug = Annotated[
    str,
    Field(
        description="Entry name, used in its URL: lowercase letters, digits and hyphens, e.g. '2026-10-03'."
    ),
]


# sites.feeds.FEEDS, which the front can't import (it needs the host extra); a test holds them equal.
SECTIONS = ("US", "Sweden", "World")

tool = hostrpc.forwarder(
    hostrpc.caller("sites", "SITES_SOCKET", "sites runner", error=ToolError),
    mcp.add_tool,
)


@tool
async def list_sites() -> str:
    """List the sites, their sections, and what fields their entries take."""


@tool
async def list_entries(
    site: Site,
    section: Annotated[
        str, Field(description="Only this section; leave empty for all.")
    ] = "",
    limit: Annotated[
        int, Field(description="At most this many entries, newest first; 0 for all.")
    ] = 20,
) -> str:
    """List a site's entries, newest first, with dates and URLs. The first line gives
    today's date in the user's time zone."""


@tool
async def get_entry(site: Site, section: Section, slug: Slug) -> str:
    """Return an entry's title, date, fields and body, e.g. to edit and write it again."""


@tool
async def write_entry(
    site: Site,
    section: Section,
    slug: Slug,
    title: Annotated[str, Field(description="Entry title.")],
    date: Annotated[str, Field(description="Entry date, YYYY-MM-DD.")],
    extra: Annotated[
        dict[str, Any],
        Field(
            description="The site's fields for this entry, as described by list_sites."
        ),
    ] = {},  # noqa: B006 - the schema's default; only passed on, never mutated
    body: Annotated[
        str, Field(description="Optional Markdown text. HTML is not allowed.")
    ] = "",
    overwrite: Annotated[
        bool, Field(description="Set true to replace an existing entry.")
    ] = False,
) -> str:
    """Save an entry and rebuild the site, so the entry is live when this returns.
    If the site doesn't build with it, nothing is saved and the error says why."""


@tool
async def delete_entry(site: Site, section: Section, slug: Slug) -> str:
    """Permanently delete an entry and rebuild the site without it."""


@tool
async def headlines(
    section: Annotated[str, Field(description=f"One of: {', '.join(SECTIONS)}.")],
) -> str:
    """Recent stories for one news section, read from reputable news feeds: up to 15,
    newest first, each with its headline, a short summary from the feed, the source, the
    article's own URL and when it was published (UTC). Duplicates are already removed."""
