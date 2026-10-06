import io
import itertools

import av
import numpy as np
import pytest
from podcasts.avio import AvCutter, AvDecoder, DecodeError
from test_fingerprint import noise, write_wav


def test_decoder_streams_what_it_decodes_and_reads_the_length(tmp_path):
    path = write_wav(tmp_path / "a.wav", noise(3, seed=1))
    d = AvDecoder()
    chunks = list(d.chunks(path, 8000))
    assert len(chunks) > 1 and all(c.ndim == 1 for c in chunks)
    assert np.array_equal(np.concatenate(chunks), d.decode(path, 8000))
    assert d.duration(path) == pytest.approx(3, abs=0.01)


def test_decoder_reports_unreadable_files(tmp_path):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"ID3 not really")
    with pytest.raises(DecodeError):
        AvDecoder().duration(bad)
    with pytest.raises(DecodeError):
        list(AvDecoder().chunks(bad, 8000))


def make_mp3(seconds: float) -> bytes:
    """A real MP3 (a tone, titled), so what is served can be cut from it."""
    buf = io.BytesIO()
    with av.open(buf, "w", format="mp3") as out:
        stream = out.add_stream("libmp3lame", rate=44100, layout="mono")
        t = np.arange(int(seconds * 44100)) / 44100
        samples = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        out.metadata["title"] = "An episode"
        for start in range(0, len(samples), 1152):
            frame = av.AudioFrame.from_ndarray(
                samples[None, start : start + 1152], format="flt", layout="mono"
            )
            frame.sample_rate = 44100
            for pkt in stream.encode(frame):
                out.mux(pkt)
        for pkt in stream.encode(None):
            out.mux(pkt)
    return buf.getvalue()


def test_cutter_removes_the_spans_without_re_encoding(tmp_path):
    (src := tmp_path / "in.mp3").write_bytes(make_mp3(30))
    removed = AvCutter().cut(src, tmp_path / "out.mp3", [(5, 10), (20, 22)])
    assert removed == pytest.approx(7, abs=0.1)
    assert AvDecoder().duration(tmp_path / "out.mp3") == pytest.approx(
        AvDecoder().duration(src) - 7, abs=0.1
    )
    with av.open(str(tmp_path / "out.mp3")) as f, av.open(str(src)) as g:
        assert f.metadata.get("title") == "An episode"
        # The packets are the very ones of the original.
        assert {bytes(p) for p in f.demux() if p.size} <= {
            bytes(p) for p in g.demux() if p.size
        }


def test_cutter_reports_unreadable_files(tmp_path):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"ID3 not really")
    with pytest.raises(DecodeError):
        AvCutter().cut(bad, tmp_path / "out.mp3", [(0, 1)])


def test_cutter_cuts_to_the_end(tmp_path):
    (src := tmp_path / "in.mp3").write_bytes(make_mp3(30))
    removed = AvCutter().cut(src, tmp_path / "out.mp3", [(25, 60)])
    assert removed == pytest.approx(5, abs=0.1)
    assert AvDecoder().duration(tmp_path / "out.mp3") == pytest.approx(
        AvDecoder().duration(src) - 5, abs=0.1
    )


class _NoDurations:
    """A container whose packets don't know their duration, as some demuxers' don't."""

    def __init__(self, container):
        self._c = container

    def __enter__(self):
        self._c.__enter__()
        return self

    def __exit__(self, *exc):
        return self._c.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._c, name)

    def demux(self, *args):
        for pkt in self._c.demux(*args):
            pkt.duration = 0
            yield pkt


def test_cutter_shifts_by_the_gap_when_packets_have_no_duration(tmp_path, monkeypatch):
    (src := tmp_path / "in.mp3").write_bytes(make_mp3(30))
    real = av.open
    monkeypatch.setattr(
        av,
        "open",
        lambda path, *a, **k: (
            _NoDurations(real(path)) if not a and not k else real(path, *a, **k)
        ),
    )
    removed = AvCutter().cut(src, tmp_path / "out.mp3", [(5, 10)])
    monkeypatch.undo()
    assert removed == pytest.approx(5, abs=0.1)
    with av.open(str(tmp_path / "out.mp3")) as f:
        stamps = [p.pts * p.time_base for p in f.demux() if p.size]  # ty: ignore[unsupported-operator]
    # No 5 s hole where the cut was: the packets after it moved up.
    assert max(b - a for a, b in itertools.pairwise(stamps)) < 0.1
