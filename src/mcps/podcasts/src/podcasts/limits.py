"""The settings a podcast's subscription takes, shared by the MCP server in the container
(for its tools' parameters) and the library on the host. Nothing heavy is imported here,
so the server needs none of the audio stack."""

from typing import Literal

DEFAULT_KEEP = 5
MAX_KEEP = 100
KeepAll = Literal["all"]
KEEP_ALL: KeepAll = "all"  # `keep` for a whole catalog
DAILY_DOWNLOADS = 30  # new episodes per feed per day, in the user's time zone
AdWords = Literal[
    "report", "cut", "off"
]  # what to do with sponsor reads found in a transcript
