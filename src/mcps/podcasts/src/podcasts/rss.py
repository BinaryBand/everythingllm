"""Read a podcast's RSS feed, and write the private feed that points at our copies.

Only RSS 2.0 is read, which is what podcast feeds are. The feed we write is built
from scratch rather than edited from the original, so tags that would send a
podcast app back to the public feed (itunes:new-feed-url, atom:link rel=self)
never reach it. parse() returns the move a feed announces separately, so the library can
follow it itself.
"""

import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
CONTENT = "http://purl.org/rss/1.0/modules/content/"
PODCAST = "https://podcastindex.org/namespace/1.0"
ET.register_namespace("itunes", ITUNES)
ET.register_namespace("podcast", PODCAST)


class FeedError(ValueError):
    """The feed couldn't be read; the message is shown to the model."""


@dataclass
class Episode:
    guid: str
    title: str
    url: str  # the enclosure, on the original host
    type: str = ""
    published: str = ""  # ISO 8601, UTC
    description: str = ""
    duration: str = ""
    image: str = ""
    # Set once downloaded (see audio.py); `file`, `bytes` and `duration` are what is served:
    file: str = (
        ""  # the served name, under the feed's folder: `stem` plus a hash of its cuts
    )
    bytes: int = 0
    audio: str = ""  # the original, in the state's audio/ folder
    stem: str = ""
    # Set once the ad scrubber has been over it (see scrub.py):
    scrubbed: bool = False
    ads_cut: float = 0.0  # seconds left out of what is served
    # Set by podcasts-transcribe (see transcripts.py):
    transcript: str = ""  # the .vtt file beside `file`
    transcript_error: str = ""  # why it couldn't be transcribed; not tried again
    possible_ads: list[list[float]] = field(
        default_factory=list
    )  # [start, end] of sponsor reads left in, served times


# The fields we work out ourselves, which a sync carries over from the last one.
DERIVED = (
    "file",
    "bytes",
    "audio",
    "stem",
    "scrubbed",
    "ads_cut",
    "transcript",
    "transcript_error",
    "possible_ads",
)


def _seconds(duration: str) -> float | None:
    """itunes:duration, `H:MM:SS`, `MM:SS` or seconds, in seconds; None if it is neither."""
    try:
        parts = [float(p) for p in duration.strip().split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3:
        return None
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def clock(seconds: float) -> str:
    """`H:MM:SS`, or `M:SS` under an hour."""
    minutes, secs = divmod(round(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


@dataclass
class Show:
    title: str
    link: str = ""
    description: str = ""
    author: str = ""
    image: str = ""
    episodes: list[Episode] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Show":
        # Records outlive the code that wrote them, so fields dropped since are ignored.
        eps = [Episode(**_known(Episode, e)) for e in d.get("episodes", [])]
        kw = _known(cls, d)
        kw["episodes"] = eps
        return cls(**kw)


def _known(cls, d: dict) -> dict:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in names}


def _text(el: ET.Element | None, path: str) -> str:
    found = el.find(path) if el is not None else None
    return (found.text or "").strip() if found is not None else ""


def _image(el: ET.Element) -> str:
    img = el.find(f"{{{ITUNES}}}image")
    if img is not None and img.get("href"):
        return img.get("href", "").strip()
    return _text(el, "image/url")


def _date(text: str) -> str:
    if not text:
        return ""
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse(data: bytes) -> tuple[Show, str]:
    """Parse an RSS feed; episodes without an enclosure are left out, newest first.

    Also returns the URL the feed says it moved to (itunes:new-feed-url), or "".
    """
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise FeedError(f"not valid XML: {e}") from None
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise FeedError(
            "not an RSS feed (no <rss><channel>); give the show's RSS feed URL."
        )
    show = Show(
        title=_text(channel, "title") or "Untitled podcast",
        link=_text(channel, "link"),
        description=_text(channel, "description")
        or _text(channel, f"{{{ITUNES}}}summary"),
        author=_text(channel, f"{{{ITUNES}}}author"),
        image=_image(channel),
    )
    for item in channel.findall("item"):
        enc = item.find("enclosure")
        if enc is None:
            continue
        url = (enc.get("url") or "").strip()
        if not url:
            continue
        show.episodes.append(
            Episode(
                guid=_text(item, "guid") or url,
                title=_text(item, "title") or "Untitled episode",
                url=url,
                type=(enc.get("type") or "").strip().lower(),
                published=_date(_text(item, "pubDate")),
                description=_text(item, f"{{{CONTENT}}}encoded")
                or _text(item, "description"),
                duration=_text(item, f"{{{ITUNES}}}duration"),
                image=_image(item),
            )
        )
    # Feeds are usually newest first, but not always; undated episodes go last.
    show.episodes.sort(key=lambda e: e.published, reverse=True)
    return show, _text(channel, f"{{{ITUNES}}}new-feed-url")


def render(show: Show, source: str, base_url: str) -> bytes:
    """Our feed: the show's details plus the downloaded episodes, served from base_url."""
    rss = ET.Element("rss", {"version": "2.0"})
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = show.title
    ET.SubElement(ch, "link").text = show.link or source
    ET.SubElement(ch, "description").text = show.description
    if show.author:
        ET.SubElement(ch, f"{{{ITUNES}}}author").text = show.author
    if show.image:
        ET.SubElement(ch, f"{{{ITUNES}}}image", {"href": show.image})
        img = ET.SubElement(ch, "image")
        ET.SubElement(img, "url").text = show.image
        ET.SubElement(img, "title").text = show.title
        ET.SubElement(img, "link").text = show.link or source
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(
        datetime.now(timezone.utc)
    )
    for ep in show.episodes:
        it = ET.SubElement(ch, "item")
        ET.SubElement(it, "title").text = ep.title
        ET.SubElement(it, "guid", {"isPermaLink": "false"}).text = ep.guid
        if ep.published:
            ET.SubElement(it, "pubDate").text = format_datetime(
                datetime.fromisoformat(ep.published)
            )
        ET.SubElement(it, "description").text = ep.description
        ET.SubElement(
            it,
            "enclosure",
            {
                "url": base_url.rstrip("/") + "/" + ep.file,
                "length": str(ep.bytes),
                "type": ep.type or "audio/mpeg",
            },
        )
        if ep.duration:
            ET.SubElement(it, f"{{{ITUNES}}}duration").text = ep.duration
        if ep.transcript:
            ET.SubElement(
                it,
                f"{{{PODCAST}}}transcript",
                {
                    "url": base_url.rstrip("/") + "/" + ep.transcript,
                    "type": "text/vtt",
                },
            )
        if ep.image:
            ET.SubElement(it, f"{{{ITUNES}}}image", {"href": ep.image})
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)
