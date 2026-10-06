"""Search the episodes' transcripts for words or a phrase (the search_podcasts tool)."""

import re
from pathlib import Path

from podcasts.audio import AudioStore
from podcasts.library import Library, clock
from podcasts.segments import Segment, TranscriptStore, retime

MAX_HITS = 20
WINDOW = 3  # segments read together, so a phrase split across two is still found
SNIPPET = 200  # characters

# The MCP server lives long, so each transcript is read and normalized once, until it or its
# cuts change: its mtime and the cuts, the episode, its segments in served times (what was
# cut left out), each one's normalized text, and every word in it.
_cache: dict[Path, tuple[tuple, dict, list[Segment], list[str], set[str]]] = {}


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w']+", " ", text.lower()).split())


def _load(
    file: Path, audio: AudioStore
) -> tuple[dict, list[Segment], list[str], set[str]] | None:
    """The cached transcript in `file` (see _cache), read again if it or its cuts changed;
    None if unreadable. The file is named by the original's hash, as its sidecar is."""
    try:
        cuts = audio.active(file.stem)
        mtime = (file.stat().st_mtime, tuple(cuts))
        cached = _cache.get(file)
        if cached is None or cached[0] != mtime:
            ep, segs = TranscriptStore.load(file)
            segs = retime(segs, cuts)
            norms = [_norm(s.text) for s in segs]
            cached = _cache[file] = (
                mtime,
                ep,
                segs,
                norms,
                set(" ".join(norms).split()),
            )
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return cached[1:]


def search(lib: Library, query: str, slug: str = "") -> tuple[list[str], int, int]:
    """Up to MAX_HITS lines `- show — episode (date) at time: snippet`, newest episode first,
    then the number of hits in all and of episodes searched. A window of segments matches
    on the exact phrase, or else on every one of its words."""
    phrase = _norm(query)
    if not phrase:
        return [], 0, 0
    terms = set(phrase.split())
    store = TranscriptStore(lib.state / "transcripts")
    hits: list[tuple[str, float, str]] = []
    searched = 0
    seen: set[Path] = set()
    for show in [slug] if slug else sorted(lib.feeds()):
        rec = lib.record(show)
        title = rec["show"].title if rec else show
        for file in store.files(show):
            seen.add(file)
            loaded = _load(file, lib.audio)
            if loaded is None:
                continue
            t, segs, norms, words = loaded
            searched += 1
            if not terms <= words:
                continue  # both ways of matching need every word
            i = 0
            while i < len(segs):
                window = " ".join(norms[i : i + WINDOW])
                if f" {phrase} " not in f" {window} " and not terms <= set(
                    window.split()
                ):
                    i += 1
                    continue
                # Start the snippet at the first segment with a word of the query in it.
                first = next(
                    (
                        j
                        for j in range(i, min(i + WINDOW, len(segs)))
                        if terms & set(norms[j].split())
                    ),
                    i,
                )
                text = " ".join(s.text for s in segs[first : i + WINDOW])
                if len(text) > SNIPPET:
                    text = text[:SNIPPET].rsplit(" ", 1)[0] + " …"
                line = f"- {title} — {t['title']} ({t['published'][:10]}) at {clock(segs[first].start)}: {text}"
                hits.append((t["published"], -segs[first].start, line))
                i += WINDOW  # one mention, found from several windows, counts once
    for gone in [f for f in _cache.keys() - seen if not f.exists()]:
        del _cache[gone]  # pruned or removed since it was read
    hits.sort(reverse=True)
    return [line for _, _, line in hits[:MAX_HITS]], len(hits), searched
