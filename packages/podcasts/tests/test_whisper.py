import wave
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from podcasts.segments import Segment, Transcript, TranscriptFormat, render_transcript
from podcasts.whisper import (
    RATE,
    InvalidTranscriptionError,
    ModelSize,
    TranscriptionError,
    TranscriptionRequest,
    WhisperTranscriber,
    decode,
    transcribe,
    windows,
)


def write_wav(path: Path, seconds: float, rate: int = 44100) -> Path:
    t = np.arange(int(seconds * rate)) / rate
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        tone = (0.2 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
        w.writeframes(np.repeat(tone, 2).tobytes())
    return path


class FakeEngine:
    def __init__(self, fail_at: int | None = None):
        self.heard: list[np.ndarray] = []
        self.languages: list[str | None] = []
        self.fail_at = fail_at

    def transcribe(self, audio, *, language, initial_prompt, vad_filter):
        self.heard.append(audio)
        self.languages.append(language)

        def segments():
            for i in range(3):
                if i == self.fail_at:
                    raise RuntimeError("decoder broke")
                yield SimpleNamespace(
                    start=i * 2.0, end=i * 2.0 + 1.5, text=f"  line {i} "
                )

        return segments(), SimpleNamespace(language="en", duration=6.0)


def transcriber(tmp_path, engine, loads: list, decoder=decode, **windows):
    def load(path):
        loads.append(path)
        return engine

    def fetch(repo, dest):
        dest.mkdir(parents=True)
        for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
            (dest / name).write_text("x")

    return WhisperTranscriber(
        tmp_path / "models",
        lambda msg: None,
        load=load,
        fetch=fetch,
        decoder=decoder,
        **windows,
    )


def test_decode_downmixes_and_resamples(tmp_path):
    length, chunks = decode(write_wav(tmp_path / "a.wav", 2))
    samples = np.concatenate(list(chunks))
    assert samples.dtype == np.float32
    assert len(samples) == pytest.approx(2 * RATE, abs=RATE * 0.01)
    assert length == pytest.approx(2, abs=0.01)


def test_transcribes_decoded_audio_and_keeps_the_engine(tmp_path):
    audio = write_wav(tmp_path / "a.wav", 1)
    engine, loads = FakeEngine(), []
    t = transcriber(tmp_path, engine, loads)
    req = TranscriptionRequest(audio, model=ModelSize.BASE)
    first = transcribe(req, t)
    transcribe(req, t)
    assert first.segments[0] == Segment(0.0, 1.5, "line 0")
    assert (first.language, len(first.segments)) == ("en", 3)
    assert first.duration == pytest.approx(
        1.0, abs=0.01
    )  # the audio's, not the engine's
    assert loads == [tmp_path / "models" / "base"]  # downloaded and loaded once
    assert isinstance(engine.heard[0], np.ndarray)


def test_engine_failure_mid_stream_is_typed(tmp_path):
    t = transcriber(tmp_path, FakeEngine(fail_at=1), [])
    with pytest.raises(TranscriptionError, match="decoder broke"):
        transcribe(TranscriptionRequest(write_wav(tmp_path / "a.wav", 1)), t)


def test_missing_audio_is_refused(tmp_path):
    with pytest.raises(InvalidTranscriptionError):
        transcribe(
            TranscriptionRequest(tmp_path / "nope.mp3"),
            transcriber(tmp_path, FakeEngine(), []),
        )


def test_vtt():
    t = Transcript("en", 4000.0, (Segment(3661.5, 3662.25, "Hello."),))
    assert (
        render_transcript(t, TranscriptFormat.VTT)
        == "WEBVTT\n\n01:01:01.500 --> 01:01:02.250\nHello.\n\n"
    )


# --- long audio, heard in windows ------------------------------------------------------


def talk(
    seconds: float, quiet_at: tuple[float, ...] = (), rate: int = RATE
) -> np.ndarray:
    """Loud noise with a near-silent second at each of `quiet_at` (seconds)."""
    audio = (
        np.random.default_rng(0).random(int(seconds * rate), dtype=np.float32) - 0.5
    ) * 0.6
    for at in quiet_at:
        audio[int(at * rate) : int((at + 1) * rate)] *= 0.001
    return audio


def in_chunks(audio: np.ndarray, size: int = 997) -> list[np.ndarray]:
    """As a decoder gives it: a frame at a time."""
    return [audio[i : i + size] for i in range(0, len(audio), size)]


R = 100  # a low rate keeps these quick; the cutting is the same at any


def cut(audio: np.ndarray) -> list[tuple[int, np.ndarray]]:
    return list(windows(in_chunks(audio), rate=R))


def test_windows_are_cut_in_the_quiet_near_each_window():
    audio = talk(50 * 60, quiet_at=(1170, 2330), rate=R)  # 19:30, then 19:20 later
    parts = cut(audio)
    starts = [a for a, _ in parts]
    assert len(parts) == 3
    assert 1170 * R <= starts[1] <= 1171 * R and 2330 * R <= starts[2] <= 2331 * R
    assert np.array_equal(
        np.concatenate([w for _, w in parts]), audio
    )  # every sample once, in order
    assert all(a + len(w) == b for (a, w), (b, _) in pairwise(parts))


def test_audio_that_fits_one_window_is_one_window():
    assert [len(w) for _, w in cut(talk(21 * 60, rate=R))] == [21 * 60 * R]
    assert [len(w) for _, w in cut(np.empty(0, np.float32))] == [0]


def test_the_last_window_is_never_a_sliver():
    # Just past the length that allows a cut, quiet at the very end: the cut can't go there.
    audio = talk(22 * 60 + 1, quiet_at=(22 * 60,), rate=R)
    parts = cut(audio)
    assert len(parts) == 2 and len(parts[-1][1]) >= 60 * R


def test_long_audio_is_heard_in_windows_with_the_first_ones_language(tmp_path):
    audio = talk(150, quiet_at=(58, 121))
    engine = FakeEngine()
    t = transcriber(
        tmp_path,
        engine,
        [],
        decoder=lambda path: (149.0, iter(in_chunks(audio, 4096))),
        window=60,
        search=5,
    )
    result = transcribe(TranscriptionRequest(write_wav(tmp_path / "a.wav", 1)), t)
    assert len(engine.heard) == 3
    assert sum(len(h) for h in engine.heard) == len(audio)  # every sample heard once
    assert engine.languages == [None, "en", "en"]  # detected once, then passed on
    first_cut = len(engine.heard[0]) / RATE
    assert 58 <= first_cut <= 59
    starts = [s.start for s in result.segments]
    assert starts[:3] == [0.0, 2.0, 4.0] and starts[3] == pytest.approx(first_cut)
    assert starts == sorted(starts)
    assert result.segments[0].text == "line 0"
    assert result.duration == pytest.approx(150)  # counted, not the header's 149
