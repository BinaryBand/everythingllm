"""What to serve for an MP3 with stretches left out, as parts of the untouched original.

`plan` drops every audio frame that starts inside a span (an MP3 frame is about 26 ms) and
describes the rest as a Manifest: byte ranges of the original file, plus the few bytes
that must be new. Those are an Info (or Xing) frame with the new frame count and seek
table, so players show the right length and seek to the right place, and the ID3 tag when
its chapters must go, since they would point at the wrong times. Nothing is re-encoded and
nothing is written but the manifest, so the original stays as it was downloaded and a
change of spans is only a new manifest.

With no spans, or none that cover a frame, the manifest is the whole original as it is.
"""

import base64
import hashlib
import json
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from splice.mp3 import INFO_TAGS, Layout, header, scan, syncsafe

CHAPTER_FRAMES = (b"CHAP", b"CTOC")


@dataclass
class Manifest:
    """What `splice-web` serves at one URL: the concatenation of `parts`, each
    `["file", name, offset, length]` (a range of a file in the audio folder) or
    `["bytes", base64]`."""

    type: str
    parts: list[list] = field(default_factory=list)
    removed: list[list[float]] = field(
        default_factory=list
    )  # [start, end] seconds of the original left out
    seconds: float | None = None  # how long the result plays, when known
    note: str = ""  # a problem making it, worth showing whenever it is served

    # Worked out once, and saved with the manifest so a reader needn't again.
    @cached_property
    def size(self) -> int:
        return sum(
            p[3] if p[0] == "file" else len(base64.b64decode(p[1])) for p in self.parts
        )

    @cached_property
    def etag(self) -> str:
        return hashlib.sha256(json.dumps(self.parts).encode()).hexdigest()[:16]

    def files(self) -> set[str]:
        return {p[1] for p in self.parts if p[0] == "file"}

    def to_json(self) -> str:
        d = {
            "type": self.type,
            "size": self.size,
            "etag": self.etag,
            "parts": self.parts,
            "removed": self.removed,
            "seconds": self.seconds,
            "note": self.note,
        }
        return json.dumps(d, indent=1) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        d = json.loads(text)
        m = cls(
            d["type"],
            d["parts"],
            d.get("removed", []),
            d.get("seconds"),
            d.get("note", ""),
        )
        for name in ("size", "etag"):
            if name in d:
                m.__dict__[name] = d[name]
        return m


def whole(
    name: str, size: int, type_: str, seconds: float | None = None, note: str = ""
) -> Manifest:
    """A manifest serving the file `name` as it is."""
    return Manifest(type_, [["file", name, 0, size]], [], seconds, note)


def plan(path: Path | str, spans, type_: str = "audio/mpeg") -> Manifest:
    """The MP3 at `path` (served under its file name) without the frames that start in
    `spans` ((start, end) seconds). Raises Mp3Error if it can't be read as an MP3."""
    path = Path(path)
    layout = scan(path)
    spans = sorted((float(a), float(b)) for a, b in spans if b > a)
    keep = _kept(layout, spans)
    if len(keep) == layout.frames:
        return whole(path.name, layout.size, type_, round(layout.seconds, 3))

    name = path.name
    parts: list[list] = []
    with open(path, "rb") as f:
        tag = f.read(layout.head)
    if tag:
        stripped = without_chapters(tag)
        if stripped is tag:
            parts.append(["file", name, 0, layout.head])
        elif stripped:
            parts.append(["bytes", base64.b64encode(stripped).decode()])
    ranges = _ranges(layout, keep)
    parts.append(["bytes", base64.b64encode(info_frame(layout, keep)).decode()])
    parts += [["file", name, a, b - a] for a, b in ranges]
    if layout.end < layout.size:
        parts.append(["file", name, layout.end, layout.size - layout.end])
    return Manifest(
        type_,
        parts,
        _removed(layout, keep),
        round(len(keep) * layout.samples / layout.rate, 3),
    )


def _kept(layout: Layout, spans: list[tuple[float, float]]) -> list[int]:
    """The frames that start outside every span."""
    keep, j = [], 0
    for i in range(layout.frames):
        t = layout.start(i)
        while j < len(spans) and spans[j][1] <= t:
            j += 1
        if not (j < len(spans) and spans[j][0] <= t):
            keep.append(i)
    return keep


def _ranges(layout: Layout, keep: list[int]) -> list[list[int]]:
    """The kept frames as [start, end) byte ranges of the original, adjacent ones joined."""
    out: list[list[int]] = []
    for i in keep:
        a, b = layout.offsets[i], layout.offsets[i] + layout.lengths[i]
        if out and out[-1][1] == a:
            out[-1][1] = b
        else:
            out.append([a, b])
    return out


def _removed(layout: Layout, keep: list[int]) -> list[list[float]]:
    """The stretches of the original left out, in seconds: the gaps between kept frames."""
    out = []
    for prev, nxt in zip([-1, *keep], [*keep, layout.frames]):
        if nxt > prev + 1:
            out.append([round(layout.start(prev + 1), 3), round(layout.start(nxt), 3)])
    return out


def info_frame(layout: Layout, keep: list[int]) -> bytes:
    """A new Xing frame (Info at a fixed bitrate) for the kept frames: their count, the
    bytes of audio (this frame included) and a seek table of 100 points."""
    hdr = bytearray(layout.first)
    hdr[1] |= 1  # no CRC
    hdr[2] &= ~2 & 0xFF  # no padding
    h = header(bytes(hdr))
    assert h is not None  # the layout's own first header, so a valid one
    # It must hold the tag, count, size and table: a larger bitrate makes a larger frame.
    while h.length < h.side_info + 120 and h.bitrate < 14:
        hdr[2] = (hdr[2] & 0x0F) | ((h.bitrate + 1) << 4)
        h = header(bytes(hdr))
        assert h is not None  # bitrate indexes up to 14 are valid
    frame = bytearray(h.length)
    frame[:4] = hdr
    total = h.length + sum(layout.lengths[i] for i in keep)
    toc, done, k = bytearray(100), h.length, 0
    for pct in range(100):
        target = len(keep) * pct // 100
        while k < target:
            done += layout.lengths[keep[k]]
            k += 1
        toc[pct] = min(255, done * 256 // total)
    varying = len({layout.bitrates[i] for i in keep}) > 1
    at = h.side_info
    frame[at : at + 4] = INFO_TAGS[0] if varying else INFO_TAGS[1]
    frame[at + 4 : at + 8] = (0x7).to_bytes(4, "big")  # frames, bytes, and toc follow
    frame[at + 8 : at + 12] = len(keep).to_bytes(4, "big")
    frame[at + 12 : at + 16] = total.to_bytes(4, "big")
    frame[at + 16 : at + 116] = toc
    return bytes(frame)


def without_chapters(tag: bytes) -> bytes:
    """The ID3v2 tag(s) `tag` without chapter frames, which cutting makes wrong.

    Returns `tag` itself when it has none. A v2.3 or v2.4 tag is rewritten without them;
    one this can't rewrite safely (v2.2, unsynchronised, an extended header, more than one
    tag) is left out whole, as are its title and art: the feed carries those anyway.
    """
    if not any(c in tag for c in CHAPTER_FRAMES):
        return tag
    if len(tag) < 10 or tag[3] not in (3, 4) or tag[5] & 0xC0:
        return b""
    size = syncsafe(tag[6:10])
    footer = 10 if tag[3] == 4 and tag[5] & 0x10 else 0
    if 10 + size + footer != len(tag):
        return b""
    body, pos = bytearray(), 10
    while pos + 10 <= 10 + size and tag[pos] != 0:
        frame_id = tag[pos : pos + 4]
        n = (
            syncsafe(tag[pos + 4 : pos + 8])
            if tag[3] == 4
            else int.from_bytes(tag[pos + 4 : pos + 8], "big")
        )
        if pos + 10 + n > 10 + size:
            return b""
        if frame_id not in CHAPTER_FRAMES:
            body += tag[pos : pos + 10 + n]
        pos += 10 + n
    if not body:
        return b""
    n = len(body)
    sizebytes = bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])
    return b"ID3" + bytes([tag[3], 0, tag[5] & ~0x10 & 0xFF]) + sizebytes + bytes(body)
