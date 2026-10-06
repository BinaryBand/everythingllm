"""Audio fingerprints for the podcasts: the stretches two or more recordings share (the
ads and promos a show repeats), found by matching spectral peaks. The `spot repeats`
command (podcasts.cli) runs the same code by hand.
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from itertools import combinations, pairwise
from pathlib import Path

import numpy as np

from podcasts.avio import Decoder


class FingerprintError(Exception):
    """Base class for every error this module raises."""


class InvalidRepeatsError(FingerprintError):
    """The files or the minimum length cannot be used."""


# Spectral peaks: the features recordings are matched on.
#
# A peak is a time-frequency cell louder than every cell within `_REACH` of it and
# than its frame's median. Peaks survive what a podcast does to a jingle -- gain
# changes, lossy re-encoding, band limiting, talk mixed over it -- because the
# jingle's loudest cells stay the loudest in their neighbourhood, where averaged
# spectra are pulled toward the speech.

RATE = 16_000
_N_FFT = 1024
HOP = 256  # 16 ms: the time resolution of a match
_BANDS = 64
_FMIN, _FMAX = 60.0, 7600.0
_FLOOR_DB = 80.0  # below the loudest cell, so digital silence holds no peaks
_BLOCK = 2048  # frames per FFT batch, bounding its temporaries to about 40 MB
_REACH = (3, 3)  # frames, bands either side that a peak must outdo


def peaks(spec: np.ndarray) -> np.ndarray:
    """A frames x bands boolean map of the peaks of a `log_mel` spectrogram."""
    median = np.median(spec, axis=1, keepdims=True)
    return (spec == _max_filter(spec, *_REACH)) & (spec > median)


def log_mel(samples: np.ndarray) -> np.ndarray:
    """A frames x bands log-power mel spectrogram of `samples` at `RATE`.

    In dB, floored `_FLOOR_DB` below its loudest cell.
    """
    if len(samples) < _N_FFT:
        return np.zeros((0, _BANDS), dtype=np.float32)
    window = np.hanning(_N_FFT).astype(np.float32)
    bank = _mel_bank()
    frames = np.lib.stride_tricks.sliding_window_view(samples, _N_FFT)[::HOP]
    spec = np.empty((len(frames), _BANDS), dtype=np.float32)
    for start in range(0, len(frames), _BLOCK):
        power = np.abs(np.fft.rfft(frames[start : start + _BLOCK] * window)) ** 2
        spec[start : start + _BLOCK] = 10 * np.log10(power @ bank + 1e-10)
    return np.maximum(spec, spec.max() - _FLOOR_DB, out=spec)


def _mel_bank() -> np.ndarray:
    """FFT bins x bands triangular filters, evenly spaced on the mel scale."""
    edges = _hz(np.linspace(_mel(_FMIN), _mel(_FMAX), _BANDS + 2))
    freqs = np.fft.rfftfreq(_N_FFT, 1 / RATE)
    low, mid, high = edges[:-2, None], edges[1:-1, None], edges[2:, None]
    rising = (freqs - low) / (mid - low)
    falling = (high - freqs) / (high - mid)
    return np.maximum(0, np.minimum(rising, falling)).T.astype(np.float32)


def _mel(hz: np.ndarray | float) -> np.ndarray:
    return 2595 * np.log10(1 + np.asarray(hz) / 700)


def _hz(mel: np.ndarray) -> np.ndarray:
    return 700 * (10 ** (mel / 2595) - 1)


def _max_filter(grid: np.ndarray, frames: int, bands: int) -> np.ndarray:
    """The maximum of each cell's neighbourhood, `frames` and `bands` either side.

    One pass along time, then one along bands: the same result as the full
    neighbourhood in `frames + bands` steps instead of their product.
    """
    out = grid
    for axis, reach in ((0, frames), (1, bands)):
        source, out = out, out.copy()
        src, dst = np.moveaxis(source, axis, 0), np.moveaxis(out, axis, 0)
        for d in range(1, reach + 1):
            np.maximum(dst[d:], src[:-d], out=dst[d:])
            np.maximum(dst[:-d], src[d:], out=dst[:-d])
    return out


# Request and result value objects.

# Four seconds is shorter than any ad seen in three podcasts and longer than the
# chance runs of shared hashes between unrelated speech.
DEFAULT_MIN_LENGTH = 4.0


@dataclass(frozen=True, slots=True)
class RepeatsRequest:
    """Find the audio two or more of `audio` share, in runs of at least `min_length` seconds."""

    audio: tuple[Path, ...]
    min_length: float = DEFAULT_MIN_LENGTH


@dataclass(frozen=True, slots=True)
class Repeat:
    """A stretch of one recording, in seconds, that also plays in each recording of `also`."""

    start: float
    end: float
    also: tuple[Hashable, ...]  # the other recordings' paths, or whatever names them


@dataclass(frozen=True, slots=True)
class Recording:
    """One recording of a `RepeatsRequest` and its repeats, earliest first."""

    audio: Path
    repeats: tuple[Repeat, ...]


# The stretches of audio that two or more recordings share. Each spectral peak is paired
# with the next `_FAN` peaks less than `_AHEAD` frames later, and a pair's two bands and
# its gap in frames make a hash. Two recordings that share a stretch of audio share many
# hashes at one constant offset between them; unrelated audio shares hashes too, but at
# scattered offsets. Hits are counted per bin of about a second and per offset, busy
# bins at one offset are chained into runs. A run's edges are its first and last hit
# with others close around it, so they are not tied to the bins and a stray hit outside
# the shared audio does not move them: inside a copy, hits at its offset come at 50 to
# 100 a second, and elsewhere at 1 to 3.

_FAN = 5  # partners per peak
_AHEAD = 64  # frames, about a second: how far ahead a partner may be
_COMMON = 30  # a hash in the other recording more often than this says nothing
_BIN = 64  # frames per counting bin, about a second
_MIN_HITS = 6  # hits at one offset in one bin for the bin to count
_SLACK = 2  # frames per offset slot; a run may drift one slot either way
_GAP = 2  # quiet bins a run may bridge
_NEAR = 16  # frames either side of a hit where others must back it up
_SUPPORT = 3  # other hits within `_NEAR` for a hit to mark an edge
_JOIN = 2.0  # seconds: repeats of one recording this close are merged
_FEWEST = 2  # recordings to compare


@dataclass(frozen=True, slots=True)
class _Hashes:
    """A recording's peak-pair hashes, sorted, with the frames of each pair's two peaks."""

    keys: np.ndarray
    anchors: np.ndarray
    ends: np.ndarray


def repeats(req: RepeatsRequest, decoder: Decoder) -> list[Recording]:
    """Each recording of `req` with the stretches it shares with any other, in request order."""
    _check(req)
    hashed = [_hashes(peaks(log_mel(decoder.decode(path, RATE)))) for path in req.audio]
    shortest = req.min_length * RATE / HOP
    found: list[list[tuple[int, int, int]]] = [[] for _ in req.audio]
    for i, j in combinations(range(len(hashed)), 2):
        for (a0, a1), (b0, b1) in _runs(hashed[i], hashed[j], shortest):
            found[i].append((a0, a1, j))
            found[j].append((b0, b1, i))
    return [
        Recording(path, _merge(spans, req.audio))
        for path, spans in zip(req.audio, found, strict=True)
    ]


def shared_with(target: np.ndarray, others: Sequence[np.ndarray]) -> tuple[Repeat, ...]:
    """The stretches of `target` that also play in any of `others`, earliest first.

    All are `peaks` maps, so a caller can keep them instead of decoding the audio again.
    A repeat's `also` holds the indexes into `others` it plays in.
    """
    mine = _hashes(target)
    shortest = DEFAULT_MIN_LENGTH * RATE / HOP
    spans = [
        (a0, a1, k)
        for k, other in enumerate(others)
        for (a0, a1), _ in _runs(mine, _hashes(other), shortest)
    ]
    return _merge(spans, tuple(range(len(others))))


def _check(req: RepeatsRequest) -> None:
    if len(req.audio) < _FEWEST:
        raise InvalidRepeatsError("give at least two recordings to compare")
    for path in req.audio:
        if not path.is_file():
            raise InvalidRepeatsError(f"no such file: {path}")
    if len({path.resolve() for path in req.audio}) < len(req.audio):
        raise InvalidRepeatsError("a recording is given twice")
    if not req.min_length > 0:
        raise InvalidRepeatsError(f"min length must be above 0, not {req.min_length}")


def _hashes(peak_map: np.ndarray) -> _Hashes:
    times, bands = np.nonzero(peak_map)  # in time order
    width = peak_map.shape[1]
    keys, anchors, ends = [], [], []
    for k in range(1, _FAN + 1):
        gap = times[k:] - times[:-k]
        near = (gap > 0) & (gap < _AHEAD)
        keys.append((bands[:-k][near] * width + bands[k:][near]) * _AHEAD + gap[near])
        anchors.append(times[:-k][near])
        ends.append(times[k:][near])
    key = np.concatenate(keys)
    order = np.argsort(key, kind="stable")
    return _Hashes(
        key[order], np.concatenate(anchors)[order], np.concatenate(ends)[order]
    )


def _shared(a: _Hashes, b: _Hashes) -> tuple[np.ndarray, np.ndarray]:
    """Index pairs into `a` and `b` of every hash the two share, skipping common ones."""
    lo = np.searchsorted(b.keys, a.keys, "left")
    count = np.searchsorted(b.keys, a.keys, "right") - lo
    count[count > _COMMON] = 0
    ia = np.repeat(np.arange(len(a.keys)), count)
    ib = (
        np.arange(count.sum())
        - np.repeat(np.cumsum(count) - count, count)
        + np.repeat(lo, count)
    )
    return ia, ib


def _runs(
    a: _Hashes, b: _Hashes, shortest: float
) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """The first and last frame, in `a` and in `b`, of each stretch the two share."""
    ia, ib = _shared(a, b)
    if not len(ia):
        return []
    at = a.anchors[ia]
    slot = (b.anchors[ib] - at) // _SLACK
    low = int(slot.min())
    width = int(slot.max()) - low + 1
    cells, counts = np.unique((at // _BIN) * width + (slot - low), return_counts=True)
    busy = cells[counts >= _MIN_HITS]
    chains: dict[int, list[list[int]]] = {}
    for cell in sorted(busy.tolist(), key=lambda c: (c // width, c % width)):
        bin_, off = divmod(cell, width)
        for near in (off, off - 1, off + 1):
            chain = chains.get(near)
            if chain and bin_ - chain[-1][1] <= _GAP + 1:
                chain[-1][1] = bin_
                break
        else:
            chains.setdefault(off, []).append([bin_, bin_, off])
    out = []
    for first, last, off in (run for chain in chains.values() for run in chain):
        if (last - first + 3) * _BIN < shortest:
            continue
        # One bin either side, since a stretch can start or end in a bin too quiet to count.
        hit = np.flatnonzero(
            (at >= (first - 1) * _BIN)
            & (at < (last + 2) * _BIN)
            & (np.abs(slot - (off + low)) <= 1)
        )
        hit = hit[_backed(at[hit])]
        if not len(hit):
            continue
        span_a = (int(at[hit].min()), int(a.ends[ia[hit]].max()))
        span_b = (int(b.anchors[ib[hit]].min()), int(b.ends[ib[hit]].max()))
        if min(span_a[1] - span_a[0], span_b[1] - span_b[0]) >= shortest:
            out.append((span_a, span_b))
    return out


def _backed(frames: np.ndarray) -> np.ndarray:
    """Which of `frames` have at least `_SUPPORT` others within `_NEAR` frames."""
    ordered = np.sort(frames)
    around = np.searchsorted(ordered, frames + _NEAR, "right") - np.searchsorted(
        ordered, frames - _NEAR, "left"
    )
    return around - 1 >= _SUPPORT


def _merge(
    spans: list[tuple[int, int, int]], audio: tuple[Hashable, ...]
) -> tuple[Repeat, ...]:
    """`spans` of one recording as repeats, a new one wherever the recordings it plays in change.

    An ad shared by every episode and a content warning shared by only some stay two
    repeats though they touch. A piece shorter than `_JOIN` joins its longer
    neighbour, so edges that differ slightly from pair to pair do not split a repeat.
    """
    join = _JOIN * RATE / HOP
    edges = sorted({x for start, end, _ in spans for x in (start, end)})
    pieces: list[tuple[int, int, frozenset[int]]] = []
    for lo, hi in pairwise(edges):
        others = frozenset(o for start, end, o in spans if start <= lo and hi <= end)
        if not others:
            continue
        if pieces and pieces[-1][2] == others and lo - pieces[-1][1] <= join:
            pieces[-1] = (pieces[-1][0], hi, others)
        else:
            pieces.append((lo, hi, others))
    merged = _absorb(pieces, join)
    return tuple(
        Repeat(
            round(start * HOP / RATE, 2),
            round((end + 1) * HOP / RATE, 2),
            tuple(audio[k] for k in sorted(others)),
        )
        for start, end, others in merged
    )


def _absorb(
    pieces: list[tuple[int, int, frozenset[int]]], join: float
) -> list[tuple[int, int, frozenset[int]]]:
    """`pieces` with each one shorter than `join` folded into the longer neighbour it touches."""
    out = list(pieces)
    while True:
        short = [
            i
            for i, (lo, hi, _) in enumerate(out)
            if hi - lo < join and _touching(out, i, join)
        ]
        if not short:
            return out
        i = short[0]
        near = [
            j
            for j in (i - 1, i + 1)
            if 0 <= j < len(out) and _close(out[min(i, j)], out[max(i, j)], join)
        ]
        j = max(near, key=lambda k: out[k][1] - out[k][0])
        a, b = out[min(i, j)], out[max(i, j)]
        joined = (a[0], b[1], out[j][2] | out[i][2])
        out[min(i, j) : max(i, j) + 1] = [joined]
        # Neighbours that now play in the same recordings become one.
        k = min(i, j)
        for m in (k - 1, k):
            if (
                0 <= m < len(out) - 1
                and out[m][2] == out[m + 1][2]
                and _close(out[m], out[m + 1], join)
            ):
                out[m : m + 2] = [(out[m][0], out[m + 1][1], out[m][2])]
                break


def _close(
    a: tuple[int, int, frozenset[int]], b: tuple[int, int, frozenset[int]], join: float
) -> bool:
    return b[0] - a[1] <= join


def _touching(
    pieces: list[tuple[int, int, frozenset[int]]], i: int, join: float
) -> bool:
    return (i > 0 and _close(pieces[i - 1], pieces[i], join)) or (
        i + 1 < len(pieces) and _close(pieces[i], pieces[i + 1], join)
    )
