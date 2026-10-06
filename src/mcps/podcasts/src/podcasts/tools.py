"""podcasts-runner: what the podcasts tools do, on the host. The MCP server in the
container (server.py) forwards each tool call here over hostrpc and shows the agent the
text returned. Each function in OPS is the tool of the same name; server.py describes them
to the agent. The work, and everything it needs (the feeds, the model's key, the audio
stack), stays on the host; syncs run as units of their own (see library.Units), so
restarting this service stops none.

Config (environment, from host.env and the unit):
  PODCASTS_SOCKET      socket to listen on (default <storage>/podcasts/runner.sock)
  ANYTHINGLLM_STORAGE  storage directory (default /srv/anythingllm/storage)
  PODCASTS_DIR         served directory, inside the pages site (default ~/.local/share/everythingllm/pages/public/podcasts)
  PODCASTS_STATE       subscriptions and sync state (default ~/.local/share/everythingllm/podcasts)
  PODCASTS_BASE_URL    public URL of PODCASTS_DIR (default https://<PUBLIC_HOST>:8445/podcasts)
  PODCASTS_MODELS      speech and transcription models (default ~/.local/share/everythingllm/podcasts/models)
  PODCASTS_TZ          the user's time zone, for the dates feeds' rules see (default Europe/Stockholm)
"""

import functools
import logging
import time
from collections import Counter

import hostrpc

from podcasts.find import find
from podcasts.library import Library, LibraryError, clock, make_client
from podcasts.limits import DEFAULT_KEEP, KEEP_ALL
from podcasts.rss import FeedError
from podcasts.search import search

TOOL_SECONDS = 45  # for the fetching in one tool call; AnythingLLM gives up at 60 s
MAX_SKIPPED_LISTED = 10  # per feed, in list_podcasts


@functools.cache
def lib() -> Library:
    library = Library.from_env()
    if not (library.site / "index.html").exists():
        library.write_index()
    return library


def _started(started: bool) -> str:
    if started:
        return (
            "Downloading in the background; call list_podcasts later to see progress."
        )
    return "A sync is already running; it picks up new podcasts before it finishes."


def find_podcast(query: str) -> str:
    with make_client() as client:
        found, note = find(client, query, time.monotonic() + TOOL_SECONDS)
    if not found:
        return note
    out = []
    for f in found:
        if not f.show:
            out.append(f"- {f.url}: doesn't work: {f.error}")
            continue
        s, latest = f.show, f.show.episodes[0]
        out.append(
            f"- {s.title}{f' by {s.author}' if s.author else ''}\n"
            f"  feed: {f.url}\n"
            f"  {len(s.episodes)} episodes; latest {latest.published[:10] or 'undated'}: {latest.title}"
        )
    return "\n".join(out)


def add_podcast(
    url: str,
    keep: int | str = DEFAULT_KEEP,
    slug: str = "",
    scrub_ads: bool | None = None,
    transcribe: bool | None = None,
    ad_words: str | None = None,
    rules: str | None = None,
) -> str:
    with make_client() as client:
        slug, show, new = lib().add(
            client,
            url,
            keep,
            slug,
            time.monotonic() + TOOL_SECONDS,
            scrub_ads,
            transcribe,
            ad_words,
            rules,
        )
    try:
        sync = _started(lib().start_sync(slug))
    except LibraryError as e:  # the subscription is saved either way
        sync = f"But {e}. Nothing will download until that's fixed."
    verb = "Subscribed to" if new else "Updated"
    return (
        f"{verb} '{show.title}' ({len(show.episodes)} episodes in its feed, {_keeping(keep)}; "
        f"{_settings(lib().feeds()[slug])}).\n"
        f"Private feed: {lib().feed_url(slug)}\n{sync}"
    )


def list_podcasts() -> str:
    feeds, local = lib().feeds(), lib().local_feeds()
    if not feeds and not local:
        return "No podcasts yet. Use add_podcast with a show's RSS feed URL."
    running = lib().sync_running()
    out = [f"Index: {lib().base_url}/  (sync {'running' if running else 'idle'})"]
    last = lib().last_sync()
    if last and last.get("error"):
        reason = last["error"].splitlines()[-1]
        out.append(
            f"The last sync (started {last['started']}) crashed: {reason}. Details are in sync.log."
        )
    elif last and not last.get("finished") and not running:
        out.append(
            f"The last sync (started {last['started']}) stopped before finishing; it was killed or crashed."
        )
    for slug, sub in sorted({**local, **feeds}.items()):
        rec = lib().record(slug)
        out.append(f"\n- {slug}: {rec['show'].title if rec else '(not synced yet)'}")
        out.append(f"  private feed: {lib().feed_url(slug)}")
        if slug in local:
            out.append(
                f"  made on this server from {sub['url']}, {_keeping(sub['keep'])}"
            )
        else:
            out.append(
                f"  source: {sub['url']}, {_keeping(sub['keep'])}; {_settings(sub)}"
            )
        if not rec:
            continue
        out.append(f"  last checked {rec['checked']}")
        if running and rec.get("downloading"):
            out.append(f"  working on: {rec['downloading']}")
        if rec.get("error"):
            out.append(f"  error: {rec['error']}")
        for ep in rec["show"].episodes:
            notes = [f"{ep.bytes / 2**20:.0f} MB"]
            if ep.ads_cut:
                notes.append(f"{clock(ep.ads_cut)} of ads cut{_by_source(ep)}")
            if ep.transcript:
                notes.append("transcribed")
            elif ep.transcript_error:
                notes.append(f"no transcript: {ep.transcript_error}")
            if ep.possible_ads:
                notes.append(
                    "possible sponsor reads at "
                    + ", ".join(f"{clock(a)}-{clock(b)}" for a, b in ep.possible_ads)
                )
            out.append(
                f"  * {ep.published[:10] or 'undated'} {ep.title} ({'; '.join(notes)})"
            )
        out.extend(_skipped(slug, sub, rec["show"].episodes))
    return "\n".join(out)


def search_podcasts(query: str, slug: str = "") -> str:
    if slug and slug not in lib().feeds():
        raise LibraryError(f"no podcast named '{slug}'.")
    lines, total, searched = search(lib(), query, slug)
    if not searched:
        return "No episode has a transcript yet; they are transcribed in the background after downloading."
    if not lines:
        return f"Not found in the {searched} transcribed episodes."
    more = (
        f" (showing the newest {len(lines)} of {total})" if total > len(lines) else ""
    )
    return f"Found in the {searched} transcribed episodes{more}:\n" + "\n".join(lines)


def refresh_podcasts(slug: str = "") -> str:
    feeds = lib().feeds()
    if slug and slug not in feeds:
        raise LibraryError(f"no podcast named '{slug}'.")
    if not feeds:
        return "No podcasts to refresh."
    return _started(lib().start_sync(slug))


def remove_podcast(slug: str) -> str:
    lib().remove(slug)
    return f"Removed '{slug}' and its downloads."


def _by_source(ep) -> str:
    """How many of the episode's cuts each source made: " (3 repeats, 1 ad read)"."""
    if not ep.audio:
        return ""
    counts = Counter(
        c.get("source", "") for c in lib().audio.cuts(ep.audio) if c.get("active", True)
    )
    names = {"repeat": "repeat", "ad-read": "ad read", "agent": "by the agent"}
    parts = [
        f"{n} {names.get(src, src)}{'s' if n != 1 and src != 'agent' else ''}"
        for src, n in sorted(counts.items())
    ]
    return f" ({', '.join(parts)})" if parts else ""


def _keeping(keep: int | str) -> str:
    return "keeping every episode" if keep == KEEP_ALL else f"keeping the newest {keep}"


def _settings(sub: dict) -> str:
    parts = [
        "cutting ads" if sub["scrub_ads"] else "leaving ads in",
        "transcribing" if sub["transcribe"] else "not transcribing",
    ]
    if sub["transcribe"] and sub["ad_words"] != "off":
        parts.append(
            "cutting ad reads" if sub["ad_words"] == "cut" else "reporting ad reads"
        )
    if sub["rules"]:
        parts.append(f"rules: {sub['rules']!r}")
    return ", ".join(parts)


def _skipped(slug: str, sub: dict, kept: list) -> list[str]:
    """The episodes the rules skipped, newer than the oldest kept, with the model's reasons."""
    if not sub.get("rules"):  # local feeds have none
        return []
    verdicts = lib().verdicts(slug)
    if verdicts["rules"] != sub["rules"]:
        return []
    oldest = min((e.published for e in kept), default="")
    skipped = sorted(
        (
            v
            for v in verdicts["episodes"].values()
            if not v["keep"] and v["published"] >= oldest
        ),
        key=lambda v: v["published"],
        reverse=True,
    )
    return [
        f"  skipped {v['published'][:10] or 'undated'} {v['title']} ({v['why']})"
        for v in skipped[:MAX_SKIPPED_LISTED]
    ]


# The tools block (feeds are fetched, files read and written), so hostrpc runs each in a thread.
OPS = (
    find_podcast,
    add_podcast,
    list_podcasts,
    search_podcasts,
    refresh_podcasts,
    remove_podcast,
)

runner = hostrpc.Service(
    OPS, errors=(LibraryError, FeedError), log=logging.getLogger("podcasts-runner")
)


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    lib()  # a bad config shows in the journal now, not at the first tool call
    hostrpc.run(runner, "podcasts", "PODCASTS_SOCKET")
