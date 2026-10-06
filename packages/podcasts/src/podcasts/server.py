"""MCP server: subscribe to podcasts, and download their episodes into a private feed.

A front for podcasts-runner on the host (podcasts/tools.py), which does the work: each
tool call goes to it over a Unix socket in storage, and the text it sends back is the
tool's result. Nothing here touches the feeds or the audio. Adding and removing a podcast
are skills (add-podcast, remove-podcast; `skills` below), not tools here.

Config (environment):
  PODCASTS_SOCKET  the runner's socket (default storage/everythingllm/podcasts/runner.sock, as the container sees it)
"""

from typing import Annotated, Literal

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


# Adding and removing a podcast are skills (anythingllm/agent-skills/add-podcast,
# remove-podcast, generated from these by `make skills`), not tools here: they can refuse a
# delegated task. refresh_podcasts stays a tool: it only starts a sync.
skills = hostrpc.Skills("podcasts", "PODCASTS_SOCKET")


@skills.add
async def add_podcast(
    url: Annotated[
        str,
        Field(
            description="The show's RSS feed URL (not its web page or Apple/Spotify link)."
        ),
    ],
    keep: Annotated[
        int | str,
        Field(
            description="How many of the newest episodes to keep downloaded, 1-100 (5 if left out); older ones are deleted. 'all' downloads the whole catalog (minus what rules skip), newest first and at most 30 a day, so a big one takes days."
        ),
    ] = 5,
    slug: Annotated[
        str,
        Field(
            description="Optional short name for the private feed's URL, e.g. 'hard-fork'. Defaults to the show's title."
        ),
    ] = "",
    scrub_ads: Annotated[
        bool | None,
        Field(
            description="Cut ads out of new episodes: audio that repeats across the show's episodes, such as ads and ad-break bumpers. On for new podcasts unless false; leave it out to keep the current setting."
        ),
    ] = None,
    transcribe: Annotated[
        bool | None,
        Field(
            description="Transcribe episodes, for search_podcasts and the podcast app. On for new podcasts unless false; leave it out to keep the current setting."
        ),
    ] = None,
    ad_words: Annotated[
        Literal["cut", "report", "off"] | None,
        Field(
            description="Ad reads found in each episode's transcript by the default model (host-read sponsors, promos): 'cut' cuts them out (the default), 'report' only lists them in list_podcasts, 'off' ignores them. Leave it out to keep the current setting."
        ),
    ] = None,
    rules: Annotated[
        str | None,
        Field(
            description="Which episodes to download, in plain words; the default model reads each episode's title, description, weekday, date and length against them. E.g. 'Skip weekend episodes', 'Only the nightly episodes Jon Stewart hosts; skip compilations, recaps and archive episodes'. Write what the user said, not keywords. Skipped episodes aren't downloaded and don't count toward keep; list_podcasts shows why each was skipped. Replaces the current rules; '' downloads every episode; leave it out to keep the current setting."
        ),
    ] = None,
) -> str:
    """Add a podcast (show) to the podcast list, i.e. subscribe to it: its newest episodes are
    downloaded to this server and served as a private feed on the tailnet, with their ads cut
    out. Calling it again with the same URL changes keep (and scrub_ads, e.g. to turn ad
    cutting off or on, or rules). Returns the private feed URL right away; downloads continue
    in the background. Get the RSS URL from the podcasts tools' find_podcast first."""


@skills.add
async def remove_podcast(
    slug: Annotated[
        str,
        Field(
            description="The podcast's slug, from the podcasts tools' list_podcasts."
        ),
    ],
) -> str:
    """Unsubscribe from a podcast and permanently delete its downloaded episodes and private
    feed. Ask the user first."""
