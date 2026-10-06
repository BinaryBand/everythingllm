"""The settings a podcast's subscription takes, shared by the runner's add_podcast op (which
the add-podcast skill fronts; its description repeats these numbers) and the library.
Nothing heavy is imported here."""

from typing import Literal

DEFAULT_KEEP = 5
MAX_KEEP = 100
KeepAll = Literal["all"]
KEEP_ALL: KeepAll = "all"  # `keep` for a whole catalog
DAILY_DOWNLOADS = 30  # new episodes per feed per day, in the user's time zone
AdWords = Literal[
    "report", "cut", "off"
]  # what to do with sponsor reads found in a transcript
