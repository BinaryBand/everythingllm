"""Ad reads found in a transcript by the default model (DeepSeek), to cut.

Fingerprints (scrub.py) only catch ads that repeat across episodes; host reads and ads
heard once get through. The model reads the numbered transcript, a chunk at a time, and
names the lines each ad runs over. Its answer is checked before anything is cut: line
numbers must exist and be in order, and a read must last MIN_SECONDS to MAX_SECONDS.
Reads that touch across a chunk boundary are joined.
"""

from collections.abc import Sequence

from llm import Chat, chat_json

from podcasts.library import clock
from podcasts.segments import Segment

CHUNK = 900  # lines per request: about half an hour of talk
MIN_SECONDS = 5.0
MAX_SECONDS = 6 * 60.0
JOIN = 3.0  # seconds: reads this close are one

PROMPT = """You find the advertisements in podcast transcripts so they can be cut out.

Each line of the transcript is `[number] minutes:seconds text`. Find every ad: a sponsor
read (by the hosts or produced), an ad for another podcast, show or network, a promo for a
paid or ad-free tier, and the show's own plugs for Patreon, merch, tours or live shows. Not
ads: the episode's content, its intro and theme, content warnings, credits, listener mail,
and the hosts talking about a product as part of the story.

Cover each ad whole, from its first line ("This episode is brought to you by...", "And now
a word from...") to its last (the promo code or URL, "...and now back to the show"). Give
only line numbers that appear below.

Answer with JSON only: {"ads": [{"first": N, "last": M, "what": "brand or show"}]}, or
{"ads": []} if there are none."""


class AdReadError(Exception):
    """The model's answer couldn't be used; the episode falls back to the phrase list."""


def find_ad_reads(
    chat: Chat, segments: Sequence[Segment], log=print
) -> list[tuple[float, float]]:
    """(start, end) seconds of each ad read in `segments`, earliest first."""
    reads: list[tuple[float, float]] = []
    for at in range(0, len(segments), CHUNK):
        lines = "\n".join(
            f"[{i}] {clock(s.start)} {s.text}"
            for i, s in enumerate(segments[at : at + CHUNK], at)
        )
        try:
            ads = chat_json(
                chat,
                [
                    {"role": "system", "content": PROMPT},
                    {"role": "user", "content": lines},
                ],
            )["ads"]
        except (ValueError, KeyError, TypeError) as e:
            raise AdReadError(
                f"the model's answer wasn't the JSON asked for: {e}"
            ) from None
        for ad in ads if isinstance(ads, list) else []:
            first, last = (
                (ad.get("first"), ad.get("last"))
                if isinstance(ad, dict)
                else (None, None)
            )
            if not (
                isinstance(first, int)
                and isinstance(last, int)
                and at <= first <= last < at + CHUNK
                and last < len(segments)
            ):
                log(f"  ignored an ad the model placed at lines {first}-{last}")
                continue
            start, end = segments[first].start, segments[last].end
            if not MIN_SECONDS <= end - start <= MAX_SECONDS:
                log(
                    f"  ignored an ad of {clock(end - start)} at {clock(start)}: too short or long to be one read"
                )
                continue
            reads.append((start, end))
    reads.sort()
    joined: list[tuple[float, float]] = []
    for start, end in reads:
        if joined and start - joined[-1][1] <= JOIN:
            joined[-1] = (joined[-1][0], max(end, joined[-1][1]))
        else:
            joined.append((start, end))
    return [(round(a, 2), round(b, 2)) for a, b in joined]
