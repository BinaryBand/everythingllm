"""Sponsor reads found by their wording in a transcript.

The ad scrubber (scrub.py) only catches audio that repeats across episodes, so an ad heard
for the first time, or one the host reads in their own words, gets through. Those read
the same way, though: "this episode is brought to you by …, go to x.com/show and use code
SHOW for 20% off". A stretch is a sponsor read when at least MIN_HITS such phrases come
within GAP seconds of each other, and the whole lasts MIN_LENGTH to MAX_LENGTH seconds.
One phrase alone is not enough: hosts say "dot com" in passing too.
"""

import re
from collections.abc import Sequence

from podcasts.segments import Segment

TRIGGERS = re.compile(
    r"brought to you by|sponsored by|(?:this|today's) (?:episode|show|podcast) is (?:sponsored|supported|brought)"
    r"|support for (?:this|the) (?:show|episode|podcast)|thanks to our sponsors?"
    r"|promo code|offer code|discount code|use (?:the )?code\b|at checkout"
    r"|\w\.com/\w|dot com slash|\.com slash"
    r"|free trial|terms (?:and conditions )?apply|first (?:month|order|box) free"
    r"|\d+ ?% off|percent off|free shipping|sign up (?:now|today)|download (?:it|the app) (?:now|today)"
    r"|we'll be right back|after (?:this|these) (?:short |quick )?(?:break|messages?|words)",
    re.IGNORECASE,
)
MIN_HITS = 2
GAP = 60.0  # seconds between one hit and the next of the same read
MIN_LENGTH = 15.0
MAX_LENGTH = 180.0


def sponsor_spans(segments: Sequence[Segment]) -> list[tuple[float, float]]:
    """The (start, end) seconds of each sponsor read in `segments`, earliest first."""
    hits = [s for s in segments if TRIGGERS.search(s.text)]
    groups: list[list[Segment]] = []
    for s in hits:
        if groups and s.start - groups[-1][-1].end <= GAP:
            groups[-1].append(s)
        else:
            groups.append([s])
    spans = []
    for group in groups:
        start, end = group[0].start, group[-1].end
        if len(group) >= MIN_HITS and MIN_LENGTH <= end - start <= MAX_LENGTH:
            spans.append((round(start, 2), round(end, 2)))
    return spans
