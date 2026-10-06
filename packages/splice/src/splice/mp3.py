"""Where everything is in an MP3 file, found from the bytes alone.

An MP3 is an ID3v2 tag, then MPEG audio frames back to back, then perhaps an APE tag and
an ID3v1 tag. Each frame starts with a 4-byte header that gives its length and how many
samples it holds, so walking the headers finds every frame without decoding any audio. The
first frame may be a Xing, Info or VBRI frame instead: no sound, just the frame count and a
seek table for players.

Only MPEG layer III is read, at a fixed bitrate or a varying one, but not free format
(whose frames don't say their length). A header is only believed when the next one lines
up with it, and bytes between frames that aren't one are skipped, as players skip them.
"""

import mmap
from array import array
from dataclasses import dataclass
from functools import cached_property, lru_cache
from pathlib import Path

BITRATES = {  # kbit/s by bitrate index, layer III
    1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
RATES = {1: (44100, 48000, 32000), 2: (22050, 24000, 16000), 25: (11025, 12000, 8000)}
VERSIONS = {3: 1, 2: 2, 0: 25}  # the header's version bits; 1 is reserved
INFO_TAGS = (b"Xing", b"Info")


class Mp3Error(ValueError):
    """Not an MP3 this module can take apart; the message says why."""


@dataclass(frozen=True)
class Header:
    version: int  # 1, 2 or 25 (MPEG 2.5)
    crc: bool  # a 2-byte CRC follows the header
    bitrate: int  # index, 1-14
    rate: int  # samples a second
    padding: bool
    mono: bool

    @property
    def samples(self) -> int:
        return 1152 if self.version == 1 else 576

    @cached_property
    def length(self) -> int:
        kbps = BITRATES[1 if self.version == 1 else 2][self.bitrate]
        return (
            144 if self.version == 1 else 72
        ) * kbps * 1000 // self.rate + self.padding

    @cached_property
    def side_info(self) -> int:
        """Where a Xing or Info tag starts in the frame: after the header, CRC and side info."""
        if self.version == 1:
            side = 17 if self.mono else 32
        else:
            side = 9 if self.mono else 17
        return 4 + 2 * self.crc + side

    def same_stream(self, other: "Header") -> bool:
        return (self.version, self.rate) == (other.version, other.rate)


@lru_cache(maxsize=4096)
def header(b: bytes) -> Header | None:
    """The layer III frame header in the 4 bytes `b`, or None if they aren't one. A file has
    only a few different headers, so each is worked out once."""
    if len(b) < 4 or b[0] != 0xFF or b[1] & 0xE0 != 0xE0:
        return None
    version = VERSIONS.get((b[1] >> 3) & 3)
    if version is None or (b[1] >> 1) & 3 != 1:  # reserved version, or not layer III
        return None
    bitrate, rate = b[2] >> 4, (b[2] >> 2) & 3
    if bitrate in (0, 15) or rate == 3:  # free format, or invalid
        return None
    return Header(
        version,
        not b[1] & 1,
        bitrate,
        RATES[version][rate],
        bool(b[2] & 2),
        b[3] >> 6 == 3,
    )


def syncsafe(b: bytes) -> int:
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


@dataclass
class Layout:
    """Where the parts of an MP3 file are: `[0, head)` the ID3v2 tag(s), `info` the Xing,
    Info or VBRI frame (offset, length) if there is one, the audio frames, and
    `[end, size)` the tags after them."""

    size: int
    head: int
    end: int
    info: tuple[int, int] | None
    first: bytes  # the first audio frame's header, to build a new Info frame from
    rate: int
    samples: int  # per frame
    offsets: array  # of each audio frame
    lengths: array
    bitrates: array  # each frame's bitrate index, to tell a varying bitrate

    @property
    def frames(self) -> int:
        return len(self.offsets)

    @property
    def seconds(self) -> float:
        return self.frames * self.samples / self.rate

    def start(self, i: int) -> float:
        """When frame `i` starts, in seconds."""
        return i * self.samples / self.rate


def _head(mm) -> int:
    """Where the ID3v2 tags at the start end (there may be more than one)."""
    pos = 0
    while mm[pos : pos + 3] == b"ID3" and pos + 10 <= len(mm):
        footer = 10 if mm[pos + 3] == 4 and mm[pos + 5] & 0x10 else 0
        pos += 10 + syncsafe(mm[pos + 6 : pos + 10]) + footer
    return min(pos, len(mm))


def _end(mm, head: int) -> int:
    """Where the tags at the end (ID3v1, APE) start."""
    end = len(mm)
    if end - head >= 128 and mm[end - 128 : end - 125] == b"TAG":
        end -= 128
    if end - head >= 32 and mm[end - 32 : end - 24] == b"APETAGEX":
        size = int.from_bytes(
            mm[end - 20 : end - 16], "little"
        )  # with the footer, not the header
        flags = int.from_bytes(mm[end - 12 : end - 8], "little")
        end -= size + (32 if flags & 0x80000000 else 0)
    return max(end, head)


def _info(mm, pos: int, h: Header) -> bool:
    """Whether the frame at `pos` is a Xing, Info or VBRI frame rather than sound."""
    at = pos + h.side_info
    return mm[at : at + 4] in INFO_TAGS or mm[pos + 36 : pos + 40] == b"VBRI"


def scan(path: Path | str) -> Layout:
    """The layout of the MP3 at `path`. Raises Mp3Error if it has no frames to find."""
    with open(path, "rb") as f:
        if not (size := f.seek(0, 2)):
            raise Mp3Error("the file is empty")
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            return _scan(mm, size)


def _scan(mm, size: int) -> Layout:
    head = _head(mm)
    end = _end(mm, head)
    offsets, lengths, bitrates = array("q"), array("H"), array("B")
    ref: Header | None = None
    first = b""
    info = None
    pos = head
    chained = -1  # where the last frame believed ends
    while pos + 4 <= end:
        h = header(mm[pos : pos + 4])
        nxt = pos + h.length if h else 0
        if h and (ref is None or h.same_stream(ref)) and nxt <= end:
            following = header(mm[nxt : nxt + 4]) if nxt + 4 <= end else None
            # Believe a header when the frame before it ended here, the next frame lines up
            # with it, or the audio ends there.
            if pos == chained or nxt == end or (following and following.same_stream(h)):
                chained = nxt
                if ref is None:
                    ref = h
                    if _info(mm, pos, h):
                        info = (pos, h.length)
                        pos = nxt
                        continue
                if not first:
                    first = bytes(mm[pos : pos + 4])
                offsets.append(pos)
                lengths.append(h.length)
                bitrates.append(h.bitrate)
                pos = nxt
                continue
        # Not a frame here: look for the next sync byte.
        pos = mm.find(b"\xff", pos + 1, end)
        if pos < 0:
            break
    if ref is None or not offsets:
        raise Mp3Error("no MP3 audio frames found")
    return Layout(
        size, head, end, info, first, ref.rate, ref.samples, offsets, lengths, bitrates
    )
