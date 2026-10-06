"""Podcast subscriptions, and the sync that downloads their episodes.

Two directories:
  state  (PODCASTS_STATE, default ~/.local/share/everythingllm/podcasts)
                           feeds.json, the subscriptions:
                             {slug: {url, keep, added, scrub_ads, transcribe, ad_words,
                                      rules}};
                           shows/<slug>.json, what the last sync saw and saved;
                           verdicts/<slug>.json, which episodes the rules keep (see rules.py);
                           downloads.json, how many episodes each feed downloaded today
                             ({slug: {day, count}});
                           audio/, cuts/ and manifests/<slug>/: each episode as downloaded,
                             what to leave out of it, and what is served (see audio.py);
                           prints/<slug>/, the ad scrubber's fingerprints (see scrub.py);
                           transcripts/<slug>/, transcripts for search (see transcripts.py);
                           sync.lock, held by the sync for as long as it runs;
                           last_sync.json, when the last sync started and finished, and
                           why it crashed or that it was stopped; sync.log, the sync's
                           output; transcribe.lock and transcribing.json (transcripts.py);
                           queue/, the syncs and passes asked for, and the workers'
                           heartbeats and schedules (see worker.py).
  site   (PODCASTS_DIR)    served by splice-web (at /podcasts/ on the pages site's port),
                           which also serves the episodes from their manifests:
                           <slug>/feed.xml, <slug>/<episode>.vtt transcripts, index.html.

A sync can take far longer than an MCP tool call may (60 s), so podcasts-runner only asks
for it: start_sync leaves a request in queue/, and podcasts-sync-worker (sync.py), a
long-running service of its own, runs it. The sync keeps its progress in shows/<slug>.json.
Each feed keeps its newest `keep` episodes (or all of them), leaving out those its `rules`
skip (the user's own words, read by the default model; see rules.py and Library.choose):
new ones are downloaded, newest first and at most DAILY_DOWNLOADS a day per feed, so a
whole catalog comes down over days without holding up the other feeds; ones that fall out
of that window are deleted, and feed.xml is rewritten after every download. A feed that
fails, or lists no episodes for now, keeps what it has; downloads pause below
MIN_FREE_BYTES of free disk.

With a Scrubber and `scrub_ads` on (the default), a feed's new episodes are downloaded
first and then have their ads found and left out, and only then join feed.xml, so a podcast
app never fetches one with its ads. Episodes already in the feed when scrubbing was turned
on stay in it while that happens. Nothing is cut from the downloaded file itself: the cuts
are a sidecar beside it, and whatever changes them calls `render`, which gives the episode a
new served name, size, duration and transcript.
"""

import fcntl
import hashlib
import html as htmllib
import itertools
import json
import os
import shutil
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args
from urllib.parse import urlsplit

import httpx
from hostrpc import atomic_write, data_dir, site_dir, storage
from llm import Chat, LLMError, deepseek, settings
from publicweb import public_client, save, stream
from publicweb import read as read_capped
from sites.store import NAME_RE as SLUG_RE
from sites.store import slugify

from podcasts.audio import GRACE, AudioStore, active
from podcasts.files import _read_json, _write_json
from podcasts.limits import DAILY_DOWNLOADS, DEFAULT_KEEP, KEEP_ALL, MAX_KEEP, AdWords
from podcasts.rss import (
    DERIVED,
    Episode,
    FeedError,
    Show,
    _seconds,
    clock,
    parse,
    render,
)
from podcasts.rules import BATCH, RuleError, judge, user_tz
from podcasts.scrub import Scrubber, ScrubError
from podcasts.segments import (
    Transcript,
    TranscriptFormat,
    TranscriptStore,
    render_transcript,
    retime,
    shift,
)
from podcasts.worker import ALL_FEEDS, SYNC_WORKER, Queue

MAX_FEED_BYTES = 20 * 1024 * 1024
MAX_EPISODE_BYTES = 2 * 1024 * 1024 * 1024
FEED_DEFAULTS = {"scrub_ads": True, "transcribe": True, "ad_words": "cut", "rules": ""}
MAX_RULES = 2000  # characters
MIN_FREE_BYTES = 20 * 2**30  # downloads pause below this much free disk
DISK_FULL = "disk nearly full (under 20 GB free); downloads paused"
NO_EPISODES = "the feed lists no episodes right now; keeping what we have"
NO_MODEL = "no DeepSeek key, so the rules can't be applied; new episodes wait"
TYPE_EXT = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/x-mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/m4a": "m4a",
    "audio/aac": "aac",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "video/mp4": "mp4",
    "video/x-m4v": "m4v",
    "video/quicktime": "mov",
}
EXT_TYPE = {ext: t for t, ext in reversed(TYPE_EXT.items())}
USER_AGENT = "everythingllm-podcasts/0.1 (private podcast mirror)"


def clean_url(text: str) -> str:
    """A URL as the user pasted it, without what the chat wraps around it: a Markdown
    link arrives as `https://x](https://x`, and angle brackets or a trailing full
    stop or bracket come along too."""
    url = text.strip().strip("<>").split("](", 1)[0]
    return url.lstrip("[(<").rstrip(".,;:)]>")


class Stopped(Exception):
    """A download cut off because the sync is stopping; the episode waits for the next."""


class LibraryError(ValueError):
    """A request the caller can fix; the message is shown to the model."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sync_lock_held(state: Path | str) -> bool:
    """Whether a sync (or a transcript being saved) holds state/sync.lock: a plain
    non-blocking flock, cheap enough for the sync worker to look while it waits."""
    with open(Path(state) / "sync.lock", "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


def make_client() -> httpx.Client:
    # Feeds come from whatever the agent was told, and downloads end up on the pages site.
    return public_client(
        FeedError,
        timeout=httpx.Timeout(30, connect=10),
        headers={"User-Agent": USER_AGENT},
    )


def read(
    client: httpx.Client, url: str, deadline: float | None = None, **params
) -> tuple[bytes, httpx.Response]:
    """The body, capped at MAX_FEED_BYTES, and the response (for its type and redirects).

    `deadline` (a time.monotonic() value) bounds the whole read, so a server that trickles
    bytes can't hold a tool call past its limit.
    """
    too_slow = f"gave up on {url}: no full answer in the time a tool call may take."
    try:
        return read_capped(
            client,
            url,
            MAX_FEED_BYTES,
            FeedError,
            deadline,
            too_slow,
            params=params or None,
        )
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise FeedError(f"couldn't fetch {url}: {e}") from None


def permanent_url(resp: httpx.Response) -> str:
    """Where the leading permanent (301/308) redirects led."""
    hops = [*resp.history, resp]
    url = str(hops[0].request.url)
    for prev, nxt in itertools.pairwise(hops):
        if prev.status_code not in (301, 308):
            break
        url = str(nxt.request.url)
    return url


def get_feed(
    client: httpx.Client, url: str, deadline: float | None = None
) -> tuple[Show, str, str]:
    """The show, its permanent URL, and the URL it says it moved to (if any)."""
    data, resp = read(client, url, deadline)
    show, moved = parse(data)
    return show, permanent_url(resp), moved


def _same_url(a: str, b: str) -> bool:
    """Equal but for the scheme and a trailing slash, which feeds often get wrong."""

    def norm(u: str) -> str:
        return u.split("://", 1)[-1].rstrip("/")

    return norm(a) == norm(b)


def follow_move(
    client: httpx.Client,
    url: str,
    show: Show,
    current: str,
    moved: str,
    deadline: float | None = None,
) -> tuple[Show, str]:
    """The show and the URL to fetch it from from now on: `current` (where permanent
    redirects led), or the announced `moved` if that feed loads. A new URL is only taken if
    its feed has episodes. Only one announced move is followed per fetch, so feeds that
    point at each other can't loop; the client checks every URL is public.
    """
    if (
        moved
        and not any(_same_url(moved, u) for u in (url, current))
        and urlsplit(moved).scheme in ("http", "https")
    ):
        try:
            new_show, new_current, _ = get_feed(client, moved, deadline)
        except FeedError:
            new_show = None
        if new_show and new_show.episodes:
            return new_show, new_current
    return show, current if show.episodes else url


def fetch_feed(
    client: httpx.Client, url: str, deadline: float | None = None
) -> tuple[Show, str]:
    """The show, and the URL to fetch it from from now on (see follow_move)."""
    return follow_move(client, url, *get_feed(client, url, deadline), deadline)


def _extension(url: str, type_: str) -> str:
    if type_ in TYPE_EXT:
        return TYPE_EXT[type_]
    suffix = Path(urlsplit(url).path).suffix.lstrip(".").lower()
    return suffix if suffix in EXT_TYPE else ""


def _prune_folder(folder: Path, keep: set[str]) -> None:
    """Delete every file in `folder` not named in `keep`, leftovers of crashed runs too."""
    for f in folder.iterdir():
        if f.is_file() and f.name not in keep:
            f.unlink()


def default_model() -> Chat | None:
    """AnythingLLM's default model, DeepSeek, with its key from AnythingLLM's .env
    (ANYTHINGLLM_ENV, default <storage>/.env); None without a key."""
    key, model = settings(str(storage() / ".env"))
    return deepseek(key, model) if key else None


def models_dir(name: str) -> Path:
    """Where the transcription models for `name` are kept."""
    return (
        Path(os.environ.get("PODCASTS_MODELS", data_dir() / "podcasts" / "models"))
        / name
    )


class Library:
    def __init__(
        self,
        site: Path | str,
        state: Path | str,
        base_url: str,
        scrubber: Scrubber | None = None,
        chat: Chat | None = None,
    ):
        self.site = Path(site)
        self.state = Path(state)
        self.base_url = base_url.rstrip("/")
        self.scrubber = scrubber  # None: ads are left in, whatever the feeds say
        self.chat = (
            chat  # the default model, for feeds' rules; None: they can't be applied
        )
        self.queue = Queue(self.state)  # where start_sync asks the sync worker
        # Asked between a sync's steps (feeds, downloads, scrubs); True stops it there: the
        # sync worker's, once it has been told to stop.
        self.stopping = lambda: False
        # Asked before each read for ads and each scrub; True leaves them for a later sync
        # (the sync worker's: worker.quiet_now, PODCASTS_QUIET_HOURS).
        self.quiet = lambda: False
        self.audio = AudioStore(self.state)
        self.transcripts = TranscriptStore(self.state / "transcripts")
        self._sync_started = ""
        self._downloads_paused = (
            False  # for the rest of this sync: the disk is nearly full
        )
        self.site.mkdir(parents=True, exist_ok=True)
        (self.state / "shows").mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "Library":
        state = os.environ.get("PODCASTS_STATE", data_dir() / "podcasts")
        host = os.environ.get("PUBLIC_HOST")
        return cls(
            os.environ.get("PODCASTS_DIR", site_dir() / "podcasts"),
            state,
            os.environ.get(
                "PODCASTS_BASE_URL",
                f"https://{host}:8445/podcasts" if host else "/podcasts",
            ),
            Scrubber(Path(state) / "prints"),
            default_model(),
        )

    def feed_url(self, slug: str) -> str:
        return f"{self.base_url}/{slug}/feed.xml"

    # --- subscriptions ---------------------------------------------------------------

    @contextmanager
    def _lock(self, name: str, blocking: bool = True):
        """Hold state/<name> (flock)."""
        with open(self.state / name, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def only_one(self, name: str):
        """Hold state/<name> unless another process does: yields False then."""
        return self._lock(name, blocking=False)

    def feeds(self) -> dict[str, dict]:
        """The subscriptions, each with every setting (FEED_DEFAULTS filling in missing ones)."""
        feeds = _read_json(self.state / "feeds.json") or {}
        return {slug: {**FEED_DEFAULTS, **sub} for slug, sub in feeds.items()}

    def cuts_ads(self, sub: dict) -> bool:
        return bool(self.scrubber and sub["scrub_ads"])

    def _save_feeds(self, feeds: dict) -> None:
        _write_json(self.state / "feeds.json", feeds)

    def add(
        self,
        client: httpx.Client,
        url: str,
        keep: int | str = DEFAULT_KEEP,
        slug: str = "",
        deadline: float | None = None,
        scrub_ads: bool | None = None,
        transcribe: bool | None = None,
        ad_words: str | None = None,
        rules: str | None = None,
    ) -> tuple[str, Show, bool]:
        """Subscribe (or change `keep`, and each setting not None, for a feed already
        subscribed). Returns (slug, show, new)."""
        if keep != KEEP_ALL and (
            not isinstance(keep, int) or not 1 <= keep <= MAX_KEEP
        ):
            raise LibraryError(
                f"keep must be between 1 and {MAX_KEEP}, or '{KEEP_ALL}'."
            )
        if ad_words is not None and ad_words not in get_args(AdWords):
            raise LibraryError(
                f"ad_words must be one of {', '.join(get_args(AdWords))}."
            )
        if rules is not None:
            rules = rules.strip()
            if len(rules) > MAX_RULES:
                raise LibraryError(f"rules takes at most {MAX_RULES} characters.")
        settings = {
            "scrub_ads": scrub_ads,
            "transcribe": transcribe,
            "ad_words": ad_words,
            "rules": rules,
        }
        settings = {k: v for k, v in settings.items() if v is not None}
        url = clean_url(url)
        if urlsplit(url).scheme not in ("http", "https"):
            raise LibraryError("url must be an http(s) URL of the show's RSS feed.")
        if slug and not SLUG_RE.fullmatch(slug):
            raise LibraryError(
                "slug must be lowercase letters, digits and hyphens, e.g. 'hard-fork'."
            )
        show, url = fetch_feed(client, url, deadline)
        if not show.episodes:
            raise LibraryError("that feed has no episodes with audio or video files.")
        with self._lock("feeds.lock"):
            feeds = self.feeds()
            existing = next((s for s, f in feeds.items() if f["url"] == url), None)
            if existing:
                feeds[existing].update(keep=keep, **settings)
                self._save_feeds(feeds)
                return existing, show, False
            slug = slug or slugify(show.title) or "podcast"
            if slug in feeds:
                raise LibraryError(
                    f"'{slug}' is already used by {feeds[slug]['url']}; pass another slug."
                )
            feeds[slug] = {
                "url": url,
                "keep": keep,
                "added": _now(),
                **FEED_DEFAULTS,
                **settings,
            }
            self._save_feeds(feeds)
        return slug, show, True

    def _move(self, slug: str, old: str, new: str) -> None:
        """Point a subscription at the feed's new URL, unless it was changed meanwhile."""
        with self._lock("feeds.lock"):
            feeds = self.feeds()
            if feeds.get(slug, {}).get("url") != old:
                return
            feeds[slug]["url"] = new
            self._save_feeds(feeds)
        print(f"{slug}: feed moved from {old} to {new}", flush=True)

    def remove(self, slug: str) -> None:
        """Unsubscribe and delete the downloads. Refused while a sync runs."""
        with self._lock("sync.lock", blocking=False) as got:
            if not got:
                raise LibraryError("a sync is running; try again once it has finished.")
            with self._lock("feeds.lock"):
                feeds = self.feeds()
                if slug not in feeds:
                    raise LibraryError(f"no podcast named '{slug}'.")
                del feeds[slug]
                self._save_feeds(feeds)
            if SLUG_RE.fullmatch(slug):
                shutil.rmtree(self.site / slug, ignore_errors=True)
                (self.state / "shows" / f"{slug}.json").unlink(missing_ok=True)
                (self.state / "verdicts" / f"{slug}.json").unlink(missing_ok=True)
                if self.scrubber:
                    self.scrubber.forget(slug)
                shutil.rmtree(self.state / "transcripts" / slug, ignore_errors=True)
                shutil.rmtree(self.audio.manifests / slug, ignore_errors=True)
                self.gc(grace=0)  # no sync is running, so nothing is half downloaded
            self.write_index()

    # --- per-show state ------------------------------------------------------------

    def record(self, slug: str) -> dict | None:
        """{show, checked, error, downloading} from the last sync, or None if never synced."""
        d = _read_json(self.state / "shows" / f"{slug}.json")
        if d is None:
            return None
        d["show"] = Show.from_dict(d["show"])
        return d

    def _save_record(
        self, slug: str, show: Show, error: str = "", downloading: str = ""
    ) -> None:
        d = {
            "show": show.to_dict(),
            "checked": _now(),
            "error": error,
            "downloading": downloading,
        }
        _write_json(self.state / "shows" / f"{slug}.json", d)

    def _publish(self, slug: str, show: Show, source: str) -> None:
        atomic_write(
            self.site / slug / "feed.xml",
            render(show, source, f"{self.base_url}/{slug}"),
        )

    def write_index(self) -> None:
        items = []
        for slug in sorted(self.feeds()):
            rec = self.record(slug)
            title = rec["show"].title if rec else slug
            n = len(rec["show"].episodes) if rec else 0
            items.append(
                f'<li><a href="{slug}/feed.xml">{htmllib.escape(title)}</a> '
                f"({n} episode{'s' if n != 1 else ''})</li>"
            )
        body = (
            "<ul>\n" + "\n".join(items) + "\n</ul>"
            if items
            else "<p>No podcasts yet.</p>"
        )
        # Plain HTML: the pages site doesn't allow inline CSS here (host/caddy/pages.Caddyfile).
        doc = (
            '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            "<title>Podcasts</title>\n</head>\n<body>\n<h1>Podcasts</h1>\n"
            "<p>Copy a feed link into a podcast app that fetches feeds from the device "
            "itself (such as AntennaPod), on a device signed in to the tailnet.</p>\n"
            f"{body}\n</body>\n</html>\n"
        )
        atomic_write(self.site / "index.html", doc)

    # --- sync --------------------------------------------------------------------------

    def sync_running(self) -> bool:
        return sync_lock_held(self.state)

    def last_sync(self) -> dict | None:
        """{started, finished, error, stopped} of the last sync; finished is "" while one
        runs, and stopped true when the sync worker was stopped before it was done."""
        try:
            return json.loads((self.state / "last_sync.json").read_text())
        except (FileNotFoundError, ValueError):
            return None

    def _save_last_sync(
        self, finished: str = "", error: str = "", stopped: bool = False
    ) -> None:
        d = {"started": self._sync_started, "finished": finished, "error": error}
        if stopped:
            d["stopped"] = True
        _write_json(self.state / "last_sync.json", d)

    def sync_crashed(self, tb: str) -> None:
        """Record a sync that died of an exception, with the end of its traceback."""
        if not self._sync_started:
            return  # died before it held the lock; last_sync.json may be another sync's
        self._save_last_sync(_now(), "\n".join(tb.strip().splitlines()[-4:]))

    def start_sync(self, slug: str = "") -> bool:
        """Ask the sync worker for a sync of `slug` (or every feed): True if it can start
        now, False if it waits for the sync that's running. Raises LibraryError when the
        worker isn't running; the request waits for it all the same."""
        self.queue.ask_sync(slug or ALL_FEEDS)
        if not self.queue.alive(SYNC_WORKER):
            raise LibraryError(
                "the sync worker isn't running (podcasts-sync-worker.service; "
                "`uv run hostctl podcasts-setup` starts it), so the sync waits until it is"
            )
        return not self.sync_running()

    def sync(self, client: httpx.Client, only: str = "") -> bool:
        """Sync every feed (or just `only`), picking up feeds added while it runs.

        Returns False without doing anything if another sync holds sync.lock. One feed failing doesn't stop the others:
        its error goes into its record. Once `stopping()` says so, it stops at the next
        feed, download or scrub, and notes in last_sync.json that it was stopped.
        """
        with self._lock("sync.lock", blocking=False) as got:
            if not got:
                return False
            self._sync_started, self._downloads_paused = _now(), False
            self._save_last_sync()
            done: set[str] = set()
            before = set(self.feeds())
            while not self.stopping():
                feeds = self.feeds()
                todo = [
                    s
                    for s in feeds
                    if s not in done and (not only or s == only or s not in before)
                ]
                if not todo:
                    break
                for slug in todo:
                    if self.stopping():
                        break
                    done.add(slug)
                    try:
                        self.sync_feed(client, slug, feeds[slug])
                    except Exception as e:  # noqa: BLE001 - one bad feed mustn't stop the rest
                        print(f"{slug}: sync failed", flush=True)
                        traceback.print_exc()
                        self._feed_failed(slug, f"sync failed: {type(e).__name__}: {e}")
            stopped = self.stopping()
            if not stopped:  # the next sync collects what this one would have
                self.gc()
            self.write_index()
            self._save_last_sync(_now(), stopped=stopped)
        return True

    def gc(self, grace: float = GRACE) -> None:
        """Delete the originals, sidecars and manifests no feed's record refers to (see
        AudioStore.gc). Run under the sync lock."""
        live: dict[str, set[str]] = {}
        originals: set[str] = set()
        for f in (self.state / "shows").glob("*.json"):
            rec = self.record(f.stem)
            for ep in rec["show"].episodes if rec else []:
                if ep.audio:
                    live.setdefault(f.stem, set()).add(ep.file)
                    originals.add(ep.audio)
        self.audio.gc(live, originals, grace)

    def _feed_failed(self, slug: str, error: str) -> None:
        try:
            rec = self.record(slug)
        except Exception:  # noqa: BLE001 - the record itself may be what broke
            rec = None
        self._save_record(slug, rec["show"] if rec else Show(title=slug), error=error)

    def sync_feed(self, client: httpx.Client, slug: str, sub: dict) -> None:
        rec = self.record(slug)
        folder = self.site / slug
        folder.mkdir(exist_ok=True)
        try:
            show, url = fetch_feed(client, sub["url"])
        except FeedError as e:
            # Keep serving what we have; just note the error.
            self._save_record(
                slug, rec["show"] if rec else Show(title=slug), error=str(e)
            )
            return
        if not show.episodes:
            # Feeds sometimes come back empty for a while; pruning to that would delete everything.
            self._save_record(slug, rec["show"] if rec else show, error=NO_EPISODES)
            return
        if url != sub["url"]:
            self._move(slug, sub["url"], url)
            sub = {**sub, "url": url}

        have = {e.guid: e for e in rec["show"].episodes} if rec else {}
        wanted, rules_error = self.choose(slug, show, sub, set(have))
        problems = []
        for ep in wanted:
            prev = have.get(ep.guid)
            if prev and self._downloaded(folder, prev):
                for name in DERIVED:
                    setattr(ep, name, getattr(prev, name))
                ep.type = ep.type or prev.type
                if prev.ads_cut:  # what we serve is shorter than the feed says
                    ep.duration = prev.duration
                # Picks up cuts changed since (by the agent, say), and a manifest gone missing.
                if problem := self.render(slug, ep):
                    problems.append(f"{ep.title}: {problem}")
        scrubbing = self.cuts_ads(sub)

        def ready(ep: Episode) -> bool:
            # In the feed already, an episode stays while it is cut; a new one waits.
            return bool(ep.file) and (
                not scrubbing or ep.scrubbed or ep.guid in have or not _scrubbable(ep)
            )

        def publish(error: str = "", downloading: str = "") -> None:
            show.episodes = [e for e in wanted if ready(e)]
            self._save_record(slug, show, error, downloading)
            self._publish(slug, show, sub["url"])

        errors = ([rules_error] if rules_error else []) + problems
        publish("; ".join(errors), next((e.title for e in wanted if not e.file), ""))
        today = datetime.now(user_tz()).date().isoformat()
        counted = (_read_json(self.state / "downloads.json") or {}).get(slug, {})
        downloaded = counted.get("count", 0) if counted.get("day") == today else 0
        for i, ep in enumerate(wanted):
            if ep.file:
                continue
            if self.stopping():  # the episode waits for the next sync
                break
            if downloaded >= DAILY_DOWNLOADS:
                waiting = sum(not e.file for e in wanted)
                errors.append(
                    f"downloaded {DAILY_DOWNLOADS} today, the daily limit; {waiting} more come in the next days"
                )
                publish("; ".join(errors))
                break
            if (
                self._downloads_paused
                or shutil.disk_usage(folder).free < MIN_FREE_BYTES
            ):
                self._downloads_paused = True
                errors.append(DISK_FULL)
                publish("; ".join(errors))
                break
            try:
                if problem := self._download(client, slug, ep):
                    errors.append(f"{ep.title}: {problem}")
            except Stopped:  # the episode waits for the next sync
                break
            except (FeedError, httpx.HTTPError, httpx.InvalidURL, OSError) as e:
                errors.append(f"{ep.title}: {e}")
            else:
                downloaded += 1
                self._count_downloads(slug, today, downloaded)
            nxt = next((e.title for e in wanted[i + 1 :] if not e.file), "")
            publish("; ".join(errors), nxt)
        if scrubbing:
            self._scrub(slug, wanted, errors, publish)

        # Episodes are served from their manifests; what's left here is feeds and transcripts.
        _prune_folder(
            folder, {e.transcript for e in wanted if e.transcript} | {"feed.xml"}
        )

    def _count_downloads(self, slug: str, day: str, count: int) -> None:
        counts = _read_json(self.state / "downloads.json") or {}
        counts[slug] = {"day": day, "count": count}
        _write_json(self.state / "downloads.json", counts)

    def verdicts(self, slug: str) -> dict:
        """{rules, episodes: {guid: {keep, why, title, published}}}: what the model made of the
        feed's episodes, under the rules it was given then."""
        return _read_json(self.state / "verdicts" / f"{slug}.json") or {
            "rules": "",
            "episodes": {},
        }

    def choose(
        self, slug: str, show: Show, sub: dict, have: set[str]
    ) -> tuple[list[Episode], str]:
        """The episodes to keep, newest first, and why some couldn't be judged ("" if all were).

        Without rules, the newest `keep` (all with KEEP_ALL). With them, the newest `keep` the model keeps: it is
        asked about BATCH episodes at a time, only as far back as needed, and only once per
        episode until the rules change. An episode it couldn't judge (no key, an answer that
        couldn't be used) stays if it was downloaded and waits if not, and so does every older
        one: an old episode fetched now would be pruned once the newer one is judged.
        """
        keep = sub.get("keep", DEFAULT_KEEP)
        limit = None if keep == KEEP_ALL else keep
        if not sub["rules"]:
            return show.episodes[:limit], ""
        saved = self.verdicts(slug)
        verdicts = saved["episodes"] if saved["rules"] == sub["rules"] else {}
        error = "" if self.chat else NO_MODEL
        wanted, blocked = [], False
        for i, ep in enumerate(show.episodes):
            if limit is not None and len(wanted) >= limit:
                break
            if ep.guid not in verdicts and not error and self.chat:
                batch = [
                    e for e in show.episodes[i : i + BATCH] if e.guid not in verdicts
                ]
                try:
                    found = judge(self.chat, sub["rules"], batch)
                except (RuleError, LLMError) as e:
                    error = f"couldn't apply the rules: {e}"
                    print(f"{slug}: {error}", flush=True)
                else:
                    for e in batch:
                        v = verdicts[e.guid] = {
                            **found[e.guid],
                            "title": e.title,
                            "published": e.published,
                        }
                        print(
                            f"{slug}: {'keep' if v['keep'] else 'skip'} {e.title}: {v['why']}",
                            flush=True,
                        )
            v = verdicts.get(ep.guid)
            if v is None:
                blocked = True
                if ep.guid in have:
                    wanted.append(ep)
            elif v["keep"] and (ep.guid in have or not blocked):
                wanted.append(ep)
        listed = {e.guid for e in show.episodes}
        (self.state / "verdicts").mkdir(exist_ok=True)
        _write_json(
            self.state / "verdicts" / f"{slug}.json",
            {
                "rules": sub["rules"],
                "episodes": {g: v for g, v in verdicts.items() if g in listed},
            },
        )
        return wanted, error

    def _scrub(
        self, slug: str, wanted: list[Episode], errors: list[str], publish
    ) -> None:
        """Find the ads in every downloaded episode of `wanted` not yet scrubbed, and leave
        them out of what is served.

        All are fingerprinted first, so a show's first sync compares its episodes with each
        other. An episode that can't be read is published as it is, and not tried again; one
        with no other episode to compare with is published as it is too.
        """
        scrubber = self.scrubber
        assert scrubber is not None  # only called when cuts_ads()
        todo = [e for e in wanted if e.audio and not e.scrubbed and _scrubbable(e)]
        for ep in todo:
            if (seconds := _seconds(ep.duration or "")) and seconds > MAX_SCRUB_SECONDS:
                errors.append(
                    f"{ep.title}: over {MAX_SCRUB_SECONDS // 3600} h, too long to look "
                    "for ads in; published as it is"
                )
                ep.scrubbed = True
        if any(e.scrubbed for e in todo):
            publish("; ".join(errors))
        todo = [e for e in todo if not e.scrubbed]
        for ep in todo:
            if self.stopping():
                return  # the rest wait for the next sync, out of the feed
            if self.quiet():
                return self._quiet(slug, todo)
            publish("; ".join(errors), f"{ep.title} (reading for ads)")
            try:
                scrubber.fingerprint(slug, key(ep), self.audio.path(ep.audio))
            except (ScrubError, OSError) as e:
                errors.append(f"{ep.title}: couldn't read it for ads: {e}")
                ep.scrubbed = True
        for i, ep in enumerate(todo):
            if ep.scrubbed:
                continue
            if self.stopping():
                return
            if self.quiet():
                return self._quiet(slug, todo)
            publish("; ".join(errors), f"{ep.title} (looking for ads)")
            try:
                spans = scrubber.scrub(slug, key(ep), self.audio.path(ep.audio))
            except (ScrubError, OSError) as e:
                errors.append(f"{ep.title}: couldn't look for its ads: {e}")
                spans = None
            ep.scrubbed = True
            if spans:
                self.audio.set_source(ep.audio, "repeat", spans)
                if problem := self.render(slug, ep):
                    errors.append(f"{ep.title}: {problem}")
                print(
                    f"{slug}: left {clock(ep.ads_cut)} of ads out of {ep.title}",
                    flush=True,
                )
            nxt = next((e.title for e in todo[i + 1 :] if not e.scrubbed), "")
            publish("; ".join(errors), f"{nxt} (looking for ads)" if nxt else "")
        scrubber.prune(slug, {key(e) for e in wanted if e.audio})

    def _quiet(self, slug: str, todo: list[Episode]) -> None:
        waiting = sum(not e.scrubbed for e in todo)
        print(
            f"{slug}: quiet hours; {waiting} episode{'s' if waiting != 1 else ''} wait "
            "for the next sync to have their ads cut",
            flush=True,
        )

    def update_episode(self, slug: str, seen: Episode, change) -> bool:
        """Call `change(episode)` under the sync lock and save and publish the result.

        For work done outside a sync (transcripts.py) on `seen`, the episode as it was when
        the work started: only if it is still there with the same original and served file,
        so a sync that cut or pruned it meanwhile isn't undone. Waits for a running sync to
        finish. False if it was gone.
        """
        with self._lock("sync.lock"):
            sub = self.feeds().get(slug)
            rec = self.record(slug)
            if not rec or not sub:
                return False
            ep = next((e for e in rec["show"].episodes if e.guid == seen.guid), None)
            if not ep or (ep.audio, ep.file, ep.bytes) != (
                seen.audio,
                seen.file,
                seen.bytes,
            ):
                return False
            if not ep.audio or not self.audio.path(ep.audio).is_file():
                return False
            change(ep)
            self._save_record(
                slug, rec["show"], rec["error"], rec.get("downloading", "")
            )
            self._publish(slug, rec["show"], sub["url"])
        return True

    def _until_stopped(self, body: Iterator[bytes]) -> Iterator[bytes]:
        """`body` until `stopping()` says so, then Stopped: a long download ends within
        the worker's stop timeout instead of being killed with its request."""
        for chunk in body:
            if self.stopping():
                raise Stopped
            yield chunk

    def _download(self, client: httpx.Client, slug: str, ep: Episode) -> str:
        """Download `ep` into the audio store and serve it as it is (for now); a problem
        rendering it, or ""."""
        stem = f"{ep.published[:10] or 'undated'}-{slugify(ep.title, 50)}-"
        stem += hashlib.sha1(ep.guid.encode()).hexdigest()[:6]
        with stream(client, ep.url, MAX_EPISODE_BYTES, FeedError) as (resp, body):
            ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype.startswith("text/"):
                raise FeedError(f"the server sent {ctype}, not audio")
            ext = _extension(ep.url, ep.type) or _extension(str(resp.url), ctype)
            if not ext:
                raise FeedError(
                    f"not a known audio or video type ({ep.type or ctype or 'none given'})"
                )
            tmp = self.audio.temp(stem, ext)
            save(self._until_stopped(body), tmp)
        ep.type = ep.type if ep.type in TYPE_EXT else EXT_TYPE[ext]
        ep.audio, ep.stem = self.audio.add(tmp, ext), stem
        return self.render(slug, ep)

    # --- what is served -------------------------------------------------------------------

    def _downloaded(self, folder: Path, ep: Episode) -> bool:
        """Whether `ep`'s original is still in the audio store."""
        return bool(ep.audio) and self.audio.path(ep.audio).is_file()

    def render(self, slug: str, ep: Episode, new_transcript: bool = False) -> str:
        """Serve `ep` as its sidecar says (AudioStore.render) and update what the feed says
        of it: served name, size, duration, cut time, reads left in, and the transcript,
        shifted to match (`new_transcript`: written even if one is there). Returns a problem
        worth noting, or "". Run under the sync lock."""
        r = self.audio.render(slug, ep.audio, ep.stem, ep.type, replacing=ep.file)
        m, side = r.manifest, r.sidecar
        ep.file, ep.bytes = r.served, m.size
        if m.seconds:
            ep.duration = clock(m.seconds)
        ep.ads_cut = round(
            side.get("legacy_cut", 0.0) + sum(b - a for a, b in m.removed), 1
        )
        ep.possible_ads = [
            [
                round(shift(c["start"], m.removed), 2),
                round(shift(c["end"], m.removed), 2),
            ]
            for c in side.get("cuts", [])
            if c.get("source") == "ad-read" and not c.get("active", True)
        ]
        if ep.transcript or new_transcript:
            self._write_vtt(slug, ep, active(side), m.removed, new_transcript)
        return m.note

    def _write_vtt(
        self, slug: str, ep: Episode, cuts, removed, force: bool = False
    ) -> None:
        """The episode's transcript as `<served stem>.vtt`, without the lines `cuts` cover and
        shifted by what was `removed` (where the cuts landed, on frames); unless `force`, not
        written again when it is there (the name changes with the cuts)."""
        name = f"{Path(ep.file).stem}.vtt"
        if not force and name == ep.transcript and (self.site / slug / name).is_file():
            return
        try:
            meta, segments = self.transcripts.load(self.transcripts.file(slug, key(ep)))
        except (OSError, ValueError, KeyError, TypeError):
            return  # no transcript to write; the old one stays
        text = render_transcript(
            Transcript(
                meta.get("language", ""), 0.0, tuple(retime(segments, cuts, removed))
            ),
            TranscriptFormat.VTT,
        )
        (self.site / slug).mkdir(exist_ok=True)
        atomic_write(self.site / slug / name, text)
        ep.transcript = name


def key(ep: Episode) -> str:
    """What the episode's fingerprint and transcript are kept under: its original's hash."""
    return Path(ep.audio or "").stem


# Looking for ads holds about 450 MB per hour of audio in the sync worker, whose
# container has 6 GB (host/quadlet/podcasts-sync-worker.container.in): a longer episode
# would get it killed at the same place on every sync, so it's published uncut.
MAX_SCRUB_SECONDS = 8 * 3600


def _scrubbable(ep: Episode) -> bool:
    """Audio only: cutting video between keyframes would break it."""
    return not (ep.type or "").startswith("video/")
