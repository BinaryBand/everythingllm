"""Transcripts as timed segments of speech: as plain text, SRT or WebVTT subtitles, or
JSON; how they fall once cuts are taken out of the audio; and the store that keeps them.
Light (the standard library and hostrpc), so what reads transcripts doesn't load Whisper
(podcasts.whisper makes them).
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from hostrpc import atomic_write

from podcasts.rss import Episode


@dataclass(frozen=True, slots=True)
class Segment:
    """A stretch of speech, in seconds from the start of the audio."""

    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class Transcript:
    language: str
    duration: float
    segments: tuple[Segment, ...]


class TranscriptFormat(StrEnum):
    TEXT = "text"
    SRT = "srt"
    VTT = "vtt"
    JSON = "json"


def render_transcript(transcript: Transcript, fmt: TranscriptFormat) -> str:
    """The transcript in `fmt`, ending with a newline unless it is empty."""
    segments = transcript.segments
    match fmt:
        case TranscriptFormat.TEXT:
            lines = [s.text for s in segments]
        case TranscriptFormat.SRT:
            lines = [f"{i}\n{_cue(s, ',')}" for i, s in enumerate(segments, 1)]
        case TranscriptFormat.VTT:
            lines = ["WEBVTT\n", *(_cue(s, ".") for s in segments)]
        case TranscriptFormat.JSON:
            return json.dumps(_json(transcript), ensure_ascii=False, indent=2) + "\n"
    return "".join(f"{line}\n" for line in lines)


def _cue(segment: Segment, mark: str) -> str:
    return f"{_stamp(segment.start, mark)} --> {_stamp(segment.end, mark)}\n{segment.text}\n"


def _stamp(seconds: float, mark: str) -> str:
    """`HH:MM:SS` then `mark` and milliseconds: `,` for SRT, `.` for WebVTT."""
    ms = round(seconds * 1000)
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{mark}{ms:03}"


def _json(transcript: Transcript) -> dict[str, object]:
    return {
        "language": transcript.language,
        "duration": transcript.duration,
        "segments": [
            {"start": s.start, "end": s.end, "text": s.text}
            for s in transcript.segments
        ],
    }


def shift(t: float, spans: Sequence[Sequence[float]]) -> float:
    """Time `t` of the original once `spans` are cut out of it."""
    return t - sum(max(0.0, min(t, end) - start) for start, end in spans)


def retime(
    segments: Sequence[Segment], cuts: Sequence[tuple[float, float]], removed=None
) -> list[Segment]:
    """`segments` as they fall once `cuts` are cut out of the audio.

    A segment inside a cut goes; the others move earlier by what was cut before them, or by
    `removed` when the cuts landed elsewhere (on MP3 frames, a few ms off).
    """
    shift_by = cuts if removed is None else removed
    return [
        Segment(
            round(shift(s.start, shift_by), 3), round(shift(s.end, shift_by), 3), s.text
        )
        for s in segments
        if not any(start <= s.start and s.end <= end for start, end in cuts)
    ]


class TranscriptStore:
    """Each episode's transcript segments, as JSON in state/transcripts/<slug>/<key>.json.

    The key is the original's hash (audio.py), and the times are in the original, so a
    transcript stays right whatever is cut: the served .vtt and search shift them by the cuts.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def file(self, slug: str, key: str) -> Path:
        return self.root / slug / f"{key}.json"

    def files(self, slug: str) -> list[Path]:
        return sorted((self.root / slug).glob("*.json"))

    def save(
        self, slug: str, key: str, ep: Episode, segments, language: str = ""
    ) -> None:
        file = self.file(slug, key)
        file.parent.mkdir(parents=True, exist_ok=True)
        d = {
            "guid": ep.guid,
            "title": ep.title,
            "published": ep.published,
            "language": language,
            "segments": [[s.start, s.end, s.text] for s in segments],
        }
        atomic_write(file, json.dumps(d, ensure_ascii=False) + "\n")

    @staticmethod
    def load(file: Path) -> tuple[dict, list[Segment]]:
        """The {guid, title, published, language} saved in `file`, and the segments."""
        d = json.loads(file.read_text())
        return d, [Segment(*s) for s in d.pop("segments")]

    def prune(self, slug: str, keys: set[str]) -> None:
        for f in self.files(slug):
            if f.stem not in keys:
                f.unlink(missing_ok=True)
