"""Speech to text with Whisper (faster-whisper), locally on the CPU, for the podcast
transcripts. The `transcribe` command (podcasts.cli) runs it by hand.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import numpy as np

from podcasts.avio import AvDecoder
from podcasts.segments import Segment, Transcript

# Typed failures raised while turning audio into text.


class TranscribeError(Exception):
    """Base class for every error this module raises."""


class InvalidTranscriptionError(TranscribeError):
    """The audio or output path cannot be used."""


class ModelFetchError(TranscribeError):
    """The model files could not be downloaded."""


class TranscriptionError(TranscribeError):
    """The engine could not load the model or read the audio."""


# Request value objects; the transcript's are in podcasts.segments.


class ModelSize(StrEnum):
    """The Whisper models on offer, smallest first."""

    BASE = "base"
    SMALL = "small"
    MEDIUM = "medium"


DEFAULT_MODEL = ModelSize.SMALL


@dataclass(frozen=True, slots=True)
class TranscriptionRequest:
    """Audio to transcribe. Left unset, `language` is detected from the first 30 seconds.

    `prompt` is text the model reads as if it came before the audio, which steers spelling
    of names and terms.
    """

    audio: Path
    model: ModelSize = DEFAULT_MODEL
    language: str | None = None
    prompt: str | None = None


@dataclass(slots=True)
class Stream:
    """What a transcriber returns: segments arrive as the audio is decoded. `duration` may
    be an estimate (a file's header) until the segments run out, then the audio's."""

    language: str
    duration: float
    segments: Iterator[Segment]


# The Whisper model files: which revision, and how to fetch them.

FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")


@dataclass(frozen=True, slots=True)
class ModelRepo:
    """A CTranslate2 Whisper model on the Hugging Face Hub, pinned to one commit."""

    repo: str
    revision: str
    size: str


REPOS = {
    ModelSize.BASE: ModelRepo(
        "Systran/faster-whisper-base",
        "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
        "150 MB",
    ),
    ModelSize.SMALL: ModelRepo(
        "Systran/faster-whisper-small",
        "536b0662742c02347bc0e980a01041f333bce120",
        "480 MB",
    ),
    ModelSize.MEDIUM: ModelRepo(
        "Systran/faster-whisper-medium",
        "08e178d48790749d25932bbc082711ddcfdfbc4f",
        "1.5 GB",
    ),
}

Fetcher = Callable[[ModelRepo, Path], None]
"""Downloads a model; returns only when every file is in place, else raises."""


def present(path: Path) -> bool:
    """Whether every model file is in `path`."""
    return all((path / name).is_file() for name in FILES)


def fetch_from_hub(repo: ModelRepo, dest: Path) -> None:
    """Download the model files of `repo` at its pinned revision into `dest`.

    Returns only when every file is there. faster-whisper's own `download_model` is not used
    because it passes `local_dir_use_symlinks`, which huggingface-hub 1.x removed.
    """
    from huggingface_hub import (
        snapshot_download,
    )

    try:
        snapshot_download(
            repo.repo,
            revision=repo.revision,
            local_dir=dest,
            allow_patterns=list(FILES),
        )
    except Exception as exc:  # network, disk and hub errors share no base class
        raise ModelFetchError(f"could not download {repo.repo}: {exc}") from exc
    if not present(dest):  # a pattern that matches nothing is not an error to the hub
        raise ModelFetchError(f"{repo.repo} downloaded without all its files to {dest}")


# `Transcriber` backed by Whisper, run locally on the CPU through faster-whisper.


class EngineSegment(Protocol):
    @property
    def start(self) -> float: ...
    @property
    def end(self) -> float: ...
    @property
    def text(self) -> str: ...


class EngineInfo(Protocol):
    @property
    def language(self) -> str: ...
    @property
    def duration(self) -> float: ...


class Engine(Protocol):
    """The slice of `faster_whisper.WhisperModel` this adapter uses."""

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str | None,  # noqa: V107 - vulture does not count keyword arguments as uses
        initial_prompt: str | None,  # noqa: V107
        vad_filter: bool,  # noqa: V107
    ) -> tuple[Iterable[EngineSegment], EngineInfo]: ...


Loader = Callable[[Path], Engine]
ChunkDecoder = Callable[
    [Path], tuple[float, Iterable[np.ndarray]]
]  # (header length, chunks)

RATE = 16_000  # what Whisper hears
# faster-whisper computes its features for all the audio it's given at once, over 1 GB an
# hour, so audio is decoded as it is heard, in windows of about this many seconds, each cut
# at the quietest moment within SEARCH seconds of where it would fall: a run holds one
# window, whatever the episode's length.
WINDOW = 20 * 60
SEARCH = 60
FRAME = 0.5  # seconds of audio whose loudness is compared when looking for a cut


def decode(path: Path) -> tuple[float, Iterator[np.ndarray]]:
    """`path`'s length by its header, and its audio as Whisper hears it, mono float32 at
    `RATE`, in chunks as they are decoded.

    Decoded with podcasts.avio's decoder, not faster-whisper's own, which passes PyAV an
    argument PyAV 15 removed.
    """
    decoder = AvDecoder()
    return decoder.duration(path), decoder.chunks(path, RATE)


def windows(
    chunks: Iterable[np.ndarray],
    rate: int = RATE,
    window: float = WINDOW,
    search: float = SEARCH,
    frame: float = FRAME,
) -> Iterator[tuple[int, np.ndarray]]:
    """`chunks` of audio regrouped into windows of about `window` seconds, each with the
    sample it starts at. Each cut is at the middle of the quietest `frame` (by RMS) within
    `search` seconds of `window` seconds in, so it falls between words, and is made only
    with `search` seconds more to come, so the last window is never a sliver: it runs from
    `search` to `window` + 2 `search` seconds. Audio that fits in one is one window, even
    none at all."""
    size, reach, step = int(window * rate), int(search * rate), int(frame * rate)
    held, count, start = [], 0, 0
    for chunk in chunks:
        held.append(chunk)
        count += len(chunk)
        if count < size + 2 * reach:
            continue
        audio = np.concatenate(held)  # once a window, not once a chunk
        stretch = audio[size - reach : size + reach]
        stretch = stretch[: len(stretch) // step * step].reshape(-1, step)
        quietest = int(
            np.argmin(np.sqrt(np.mean(np.square(stretch, dtype=np.float64), axis=1)))
        )
        cut = size - reach + quietest * step + step // 2
        yield start, audio[:cut]
        held, count, start = [audio[cut:].copy()], len(audio) - cut, start + cut
        del audio, stretch  # or they'd hold this window while the next one gathers
    yield start, np.concatenate(held) if held else np.empty(0, np.float32)


def load_whisper(path: Path, threads: int = 0) -> Engine:
    """The model at `path`, using `threads` CPU threads (0: as many as there are cores)."""
    from faster_whisper import WhisperModel

    return WhisperModel(
        str(path), device="cpu", compute_type="int8", cpu_threads=threads
    )


class WhisperTranscriber:
    """Downloads a model the first time it is asked for, then loads it from disk.

    `on_download` hears about each download before it starts, since one can take minutes.
    """

    def __init__(
        self,
        models_root: Path,
        on_download: Callable[[str], None],
        load: Loader = load_whisper,
        fetch: Fetcher = fetch_from_hub,
        decoder: ChunkDecoder = decode,
        window: float = WINDOW,
        search: float = SEARCH,
    ) -> None:
        self._root = models_root
        self._on_download = on_download
        self._load = load
        self._fetch = fetch
        self._decode = decoder
        self._window, self._search = window, search
        self._engines: dict[
            str, Engine
        ] = {}  # loading takes seconds; keep each for later calls

    def transcribe(self, req: TranscriptionRequest) -> Stream:
        engine = self._engine(req)
        try:
            estimate, chunks = self._decode(req.audio)
            parts = windows(chunks, window=self._window, search=self._search)
            # The first window now, for the language; it goes for the windows after it.
            start, audio = next(parts)
            first, info = _hear(engine, audio, start, req.language, req.prompt)
        except Exception as exc:  # PyAV and the tokenizer raise their own types
            raise TranscriptionError(f"cannot transcribe {req.audio}: {exc}") from exc
        language = req.language or info.language
        end = start + len(
            audio
        )  # not `audio` in heard(), which would hold it to the end

        def heard() -> Iterator[Segment]:
            nonlocal end
            yield from first
            for a, rest in parts:
                end, segments = (
                    a + len(rest),
                    _hear(engine, rest, a, language, req.prompt)[0],
                )
                del rest  # nor this window while windows() gathers the next
                yield from segments
            stream.duration = end / RATE  # the header's was an estimate

        stream = Stream(info.language, estimate, _segments(heard(), req.audio))
        return stream

    def _engine(self, req: TranscriptionRequest) -> Engine:
        if req.model in self._engines:
            return self._engines[req.model]
        repo, path = REPOS[req.model], self._root / req.model
        if not present(path):
            self._on_download(
                f"downloading the {req.model} model ({repo.size}) to {path}"
            )
            self._fetch(repo, path)
        try:
            engine = self._engines[req.model] = self._load(path)
        except Exception as exc:  # a corrupt or incompatible model file
            raise TranscriptionError(
                f"could not load the model from {path}: {exc}"
            ) from exc
        return engine


def _hear(
    engine: Engine,
    audio: np.ndarray,
    start: int,
    language: str | None,
    prompt: str | None,
) -> tuple[Iterator[Segment], EngineInfo]:
    """One window, which starts at sample `start`, with its segments' times in the whole
    audio's."""
    # Voice activity detection skips silence and music, where Whisper invents text.
    segments, info = engine.transcribe(
        audio, language=language, initial_prompt=prompt, vad_filter=True
    )
    offset = start / RATE
    return (
        Segment(s.start + offset, s.end + offset, s.text.strip()) for s in segments
    ), info


def _segments(heard: Iterator[Segment], audio: Path) -> Iterator[Segment]:
    """The engine decodes lazily, so its errors surface here and are typed here too."""
    try:
        yield from heard
    except Exception as exc:  # anything from the decoder or the model
        raise TranscriptionError(f"transcription of {audio} failed: {exc}") from exc


class Transcriber(Protocol):
    """Turns speech into text."""

    def transcribe(self, req: TranscriptionRequest) -> Stream:
        """Start transcribing `req.audio`. Iterating the segments does the work.

        Raises `ModelFetchError` or `TranscriptionError`, also while iterating.
        """
        ...


Progress = Callable[[float, float], None]
"""Called after each segment with the seconds transcribed and the total."""


def transcribe(
    req: TranscriptionRequest,
    transcriber: Transcriber,
    progress: Progress | None = None,
) -> Transcript:
    if not req.audio.is_file():
        raise InvalidTranscriptionError(f"no such audio file: {req.audio}")
    stream = transcriber.transcribe(req)
    segments = []
    for segment in stream.segments:
        segments.append(segment)
        if progress:
            progress(segment.end, stream.duration)
    return Transcript(stream.language, stream.duration, tuple(segments))
