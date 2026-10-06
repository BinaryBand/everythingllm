"""Audio through PyAV's bundled FFmpeg: decoding to mono float samples, and cutting
stretches out of a file without re-encoding. The fingerprints, the transcriber and the
served episodes all read and cut audio here.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from itertools import chain
from pathlib import Path
from typing import Protocol

import av
import numpy as np


class DecodeError(Exception):
    """A file could not be read or cut as audio."""


class Decoder(Protocol):
    """Reads audio files."""

    def decode(self, path: Path, rate: int) -> np.ndarray:
        """The audio of `path` as mono float32 samples at `rate` Hz. Raises `DecodeError`."""
        ...


# Cuts stretches out of an audio file through PyAV, without re-encoding.


class AvCutter:
    """Copies the first audio stream's packets, leaving out those that start in a span.

    Copying packets keeps the audio as it was, and takes about a second for an hour of
    MP3. A cut lands on a packet (about 26 ms of MP3), close enough for ads. The
    container's tags are copied; cover art and chapters are not, since the chapters
    would point at the wrong times.
    """

    def cut(self, src: Path, dst: Path, spans: Sequence[tuple[float, float]]) -> float:
        """Write `src` less `spans` (start, end in seconds) to `dst`; the seconds removed.

        `dst`'s extension picks its format, so give it the same one as `src`.
        """
        # Each run of left-out packets is measured from its first packet's timestamp to the
        # next kept one's, since a demuxer may not know a packet's duration (None or 0).
        removed = 0
        run_from: int | None = (
            None  # the timestamp the current run of cut packets starts at
        )
        last_end = 0  # where the last cut packet ends, as far as is known
        try:
            with av.open(str(src)) as inp, av.open(str(dst), "w") as out:
                if not inp.streams.audio:
                    raise DecodeError(f"{src} has no audio stream")
                ist = inp.streams.audio[0]
                ost = out.add_stream_from_template(ist)
                out.metadata.update(inp.metadata)
                for pkt in inp.demux(ist):
                    if pkt.dts is None:  # the demuxer's empty flushing packet
                        continue
                    ts = pkt.pts if pkt.pts is not None else pkt.dts
                    if any(
                        start <= float(ts * ist.time_base) < end for start, end in spans
                    ):
                        if run_from is None:
                            run_from = ts
                        last_end = ts + (pkt.duration or 0)
                        continue
                    if run_from is not None:
                        removed += ts - run_from
                        run_from = None
                    pkt.dts -= removed
                    if pkt.pts is not None:
                        pkt.pts -= removed
                    pkt.stream = ost
                    out.mux(pkt)
                if run_from is not None:  # a cut that runs to the end
                    removed += last_end - run_from
                return float(removed * ist.time_base)
        except av.FFmpegError as exc:
            raise DecodeError(f"cannot cut {src}: {exc}") from exc


# Decodes audio and video files through PyAV's bundled FFmpeg.


class AvDecoder:
    """Implements `Decoder`: the first audio stream, downmixed and resampled."""

    def decode(self, path: Path, rate: int) -> np.ndarray:
        # One growing buffer, read without a copy, where a list of chunks joined at
        # the end would hold the audio twice: about 230 MB an hour at 16 kHz.
        pcm = bytearray()
        for chunk in self.chunks(path, rate):
            pcm += memoryview(chunk)  # numpy would take `+= chunk` as an addition
        return np.frombuffer(pcm, dtype=np.float32)

    def chunks(self, path: Path, rate: int) -> Iterator[np.ndarray]:
        """`decode`'s samples a frame at a time, for a caller that needn't hold them all."""
        with _open(path) as container:
            if not container.streams.audio:
                raise DecodeError(f"{path} has no audio stream")
            resampler = av.AudioResampler(format="flt", layout="mono", rate=rate)
            for frame in chain(container.decode(audio=0), [None]):  # None flushes
                for out in resampler.resample(frame):
                    yield out.to_ndarray().reshape(-1)

    def duration(self, path: Path) -> float:
        """`path`'s length in seconds as its header gives it, an estimate for some VBR
        files; 0.0 if it gives none."""
        with _open(path) as container:
            return container.duration / av.time_base if container.duration else 0.0


@contextmanager
def _open(path: Path) -> Iterator[av.container.InputContainer]:
    """`path` opened, with PyAV's errors, reading it or after, raised as `DecodeError`."""
    try:
        with av.open(str(path)) as container:
            yield container
    except av.FFmpegError as exc:
        raise DecodeError(f"cannot read audio from {path}: {exc}") from exc
