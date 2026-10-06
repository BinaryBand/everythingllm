import wave
from pathlib import Path

import numpy as np
import pytest
from podcasts.avio import AvDecoder
from podcasts.fingerprint import (
    RATE,
    RepeatsRequest,
    log_mel,
    peaks,
    repeats,
    shared_with,
)


def noise(seconds: float, seed: int) -> np.ndarray:
    """Audio no other seed shares: speech-like enough for spectral peaks to differ throughout."""
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(int(seconds * RATE)).astype(np.float32)
    # Loudness that wanders, so peaks are not spread evenly like pure white noise.
    env = np.repeat(rng.uniform(0.2, 1.0, int(seconds * 10) + 1), RATE // 10)[
        : len(white)
    ]
    return 0.3 * white * env


def write_wav(path: Path, samples: np.ndarray) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    return path


AD = noise(12, seed=99)


def episode(seed: int, ad_at: float | None, length: float = 60) -> np.ndarray:
    talk = noise(length, seed)
    if ad_at is None:
        return talk
    i = int(ad_at * RATE)
    return np.concatenate([talk[:i], AD, talk[i:]])


def print_of(samples: np.ndarray) -> np.ndarray:
    return peaks(log_mel(samples))


def test_shared_with_finds_the_ad_wherever_it_plays():
    target = print_of(episode(1, ad_at=20))
    others = [print_of(episode(2, ad_at=40)), print_of(episode(3, ad_at=None))]
    found = shared_with(target, others)
    assert len(found) == 1
    assert found[0].start == pytest.approx(20, abs=0.5)
    assert found[0].end == pytest.approx(32, abs=0.5)
    assert found[0].also == (0,)


def test_shared_with_finds_nothing_in_unrelated_audio():
    assert shared_with(print_of(episode(1, None)), [print_of(episode(2, None))]) == ()


def test_repeats_reads_files(tmp_path):
    a = write_wav(tmp_path / "a.wav", episode(1, ad_at=10))
    b = write_wav(tmp_path / "b.wav", episode(2, ad_at=30))
    found = repeats(RepeatsRequest((a, b)), AvDecoder())
    assert [len(r.repeats) for r in found] == [1, 1]
    assert found[1].repeats[0].start == pytest.approx(30, abs=0.5)
    assert found[1].repeats[0].also == (a,)
