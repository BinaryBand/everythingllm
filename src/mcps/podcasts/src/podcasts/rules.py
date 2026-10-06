"""A feed's `rules`, read by the default model (DeepSeek), to pick which episodes to keep.

The rules are the user's own words ("only the nights Jon Stewart hosts; no compilations"),
so they can say anything an episode's title, description, date or length gives away. The
model reads a batch of episodes and answers keep or skip for each, with a reason. Its
answer must cover every episode asked about, or none of it is used.

A model may answer differently each time it is asked, and an episode that flipped from
kept to skipped would be deleted, so each verdict is asked for once and kept in
state/verdicts/<slug>.json until the rules change (see Library.choose).
"""

import html as htmllib
import os
import re
from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from llm import Chat, chat_json

from podcasts.rss import Episode, _seconds, clock

BATCH = 20  # episodes per request
DESCRIPTION_CHARS = 600
WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

PROMPT = """You choose which episodes of a podcast to download, following the listener's rules.

The rules:
<rules>
{rules}
</rules>

Each episode below is `[number] weekday date (in the listener's time zone) · length ·
title`, then the start of its description. Check every rule against every episode, using
what is given: who hosts or appears is usually named in the description. Rules that say
what to keep ("only ...") skip everything else; rules that only say what to skip keep
everything else.

Answer with JSON only, giving the reason before the verdict:
{{"episodes": [{{"n": N, "why": "a few words", "keep": true or false}}]}}, one entry for
every episode below."""


class RuleError(Exception):
    """The model's answer couldn't be used; the episodes wait for the next sync."""


def user_tz() -> ZoneInfo:
    """The user's time zone (PODCASTS_TZ, default Europe/Stockholm)."""
    return ZoneInfo(os.environ.get("PODCASTS_TZ") or "Europe/Stockholm")


def local_day(published: str) -> str:
    """The weekday and date an episode came out on in the user's time zone (PODCASTS_TZ),
    e.g. "Friday 2026-10-02", since feeds give UTC: a 9 pm Friday release in the US is
    Saturday in UTC. Worked out here because models get weekdays wrong."""
    day = datetime.fromisoformat(published).astimezone(user_tz())
    return f"{WEEKDAYS[day.weekday()]} {day.date().isoformat()}"


def _plain(text: str) -> str:
    text = htmllib.unescape(re.sub(r"<[^>]+>", " ", text))
    return " ".join(text.split())[:DESCRIPTION_CHARS]


def _line(n: int, ep: Episode) -> str:
    when = local_day(ep.published) if ep.published else "undated"
    secs = _seconds(ep.duration)
    length = clock(secs) if secs else "length unknown"
    return f"[{n}] {when} · {length} · {ep.title}\n    {_plain(ep.description)}"


def judge(chat: Chat, rules: str, episodes: Sequence[Episode]) -> dict[str, dict]:
    """{guid: {keep, why}} for each of `episodes`, by the model."""
    lines = "\n".join(_line(n, ep) for n, ep in enumerate(episodes, 1))
    system = PROMPT.format(rules=rules)
    try:
        answers = chat_json(
            chat,
            [{"role": "system", "content": system}, {"role": "user", "content": lines}],
        )["episodes"]
        verdicts = {
            a["n"]: {"keep": a["keep"], "why": str(a.get("why", ""))} for a in answers
        }
    except (ValueError, KeyError, TypeError) as e:
        raise RuleError(f"the model's answer wasn't the JSON asked for: {e}") from None
    if any(not isinstance(v["keep"], bool) for v in verdicts.values()):
        raise RuleError("the model's answer had a keep that wasn't true or false")
    if missing := [n for n in range(1, len(episodes) + 1) if n not in verdicts]:
        raise RuleError(
            f"the model left out episode{'s' if len(missing) > 1 else ''} {', '.join(map(str, missing))}"
        )
    return {ep.guid: verdicts[n] for n, ep in enumerate(episodes, 1)}
