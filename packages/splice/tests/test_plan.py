import base64
import io
from pathlib import Path

import av
import numpy as np
import pytest
from splice.mp3 import Mp3Error, header, scan
from splice.plan import Manifest, plan, whole, without_chapters

SECONDS = 20


def make_mp3(path: Path, vbr: bool = False, seconds: float = SECONDS) -> Path:
    """A chirp that stops every other second, so a varying bitrate does vary."""
    with av.open(str(path), "w") as out:
        stream = out.add_stream("libmp3lame", rate=44100, layout="mono")
        if vbr:
            stream.codec_context.qscale = True
            stream.codec_context.global_quality = 4 * 118  # -q:a 4
        t = np.arange(int(seconds * 44100)) / 44100
        samples = (
            0.2 * np.sin(2 * np.pi * 440 * t * (1 + t / 10)) * (t % 2 < 1)
        ).astype(np.float32)
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
    return path


def frame_bytes(frame_id: bytes, data: bytes) -> bytes:
    return frame_id + len(data).to_bytes(4, "big") + b"\0\0" + data


def id3v23(*frames: bytes) -> bytes:
    body = b"".join(frames)
    n = len(body)
    return (
        b"ID3\x03\x00\x00"
        + bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])
        + body
    )


TITLE = frame_bytes(b"TIT2", b"\x00An episode")
CHAPTER = frame_bytes(
    b"CHAP",
    b"ch1\x00" + (0).to_bytes(4, "big") + (5000).to_bytes(4, "big") + b"\xff" * 8,
)


def with_tag(
    src: Path, dst: Path, tag: bytes, junk: bytes = b"", tail: bytes = b""
) -> Path:
    """`src`'s audio frames under `tag` instead of its own, with `junk` after the first
    audio frame and `tail` (say an ID3v1 tag) at the end."""
    layout = scan(src)
    data = src.read_bytes()
    audio = data[layout.head :]
    if junk:
        cut = layout.offsets[0] + layout.lengths[0] - layout.head
        audio = audio[:cut] + junk + audio[cut:]
    dst.write_bytes(tag + audio + tail)
    return dst


def served(m: Manifest, folder: Path) -> bytes:
    out = bytearray()
    for p in m.parts:
        if p[0] == "file":
            with open(folder / p[1], "rb") as f:
                f.seek(p[2])
                out += f.read(p[3])
        else:
            out += base64.b64decode(p[1])
    return bytes(out)


def decoded(data: bytes) -> tuple[float, int]:
    """The duration PyAV reads, and how many packets it finds."""
    with av.open(io.BytesIO(data), "r") as f:
        assert f.duration is not None
        seconds = f.duration / av.time_base
        return seconds, sum(1 for p in f.demux(f.streams.audio[0]) if p.size)


@pytest.mark.parametrize("vbr", [False, True])
def test_scan_finds_every_frame_and_the_info_frame(tmp_path, vbr):
    path = make_mp3(tmp_path / "a.mp3", vbr)
    layout = scan(path)
    assert layout.info is not None
    assert layout.frames == decoded(path.read_bytes())[1]
    assert (len(set(layout.bitrates)) > 1) == vbr
    assert layout.seconds == pytest.approx(SECONDS, abs=0.1)


def test_no_spans_serve_the_original_byte_for_byte(tmp_path):
    path = make_mp3(tmp_path / "a.mp3")
    for spans in ([], [(SECONDS + 5, SECONDS + 9)]):
        m = plan(path, spans)
        assert (
            m.parts == [["file", "a.mp3", 0, path.stat().st_size]] and m.removed == []
        )
        assert served(m, tmp_path) == path.read_bytes()
        assert m.seconds == pytest.approx(SECONDS, abs=0.1)


@pytest.mark.parametrize("vbr", [False, True])
def test_spans_leave_out_their_frames(tmp_path, vbr):
    path = make_mp3(tmp_path / "a.mp3", vbr)
    m = plan(path, [(3, 6), (12, 13.5)])
    assert [[round(a), round(b, 1)] for a, b in m.removed] == [[3, 6.0], [12, 13.5]]
    data = served(m, tmp_path)
    assert len(data) == m.size
    seconds, packets = decoded(data)
    left = SECONDS - 4.5
    assert seconds == pytest.approx(left, abs=0.1) and m.seconds == pytest.approx(
        left, abs=0.1
    )
    # The new Info/Xing frame counts the frames that are left, and PyAV believes it.
    layout = scan_bytes(tmp_path, data)
    assert layout.info is not None and layout.frames == packets
    info = header(data[layout.info[0] : layout.info[0] + 4])
    assert info is not None
    at = layout.info[0] + info.side_info
    assert data[at : at + 4] == (b"Xing" if vbr else b"Info")
    assert int.from_bytes(data[at + 8 : at + 12], "big") == layout.frames
    assert int.from_bytes(data[at + 12 : at + 16], "big") == len(data) - layout.head


def scan_bytes(folder: Path, data: bytes):
    out = folder / "out.mp3"
    out.write_bytes(data)
    return scan(out)


def test_the_original_is_never_written(tmp_path):
    path = make_mp3(tmp_path / "a.mp3")
    before = path.read_bytes()
    plan(path, [(1, 2)])
    assert path.read_bytes() == before


def test_chapters_go_when_cutting_and_stay_otherwise(tmp_path):
    path = with_tag(
        make_mp3(tmp_path / "src.mp3"), tmp_path / "a.mp3", id3v23(TITLE, CHAPTER)
    )
    assert served(plan(path, []), tmp_path) == path.read_bytes()
    data = served(plan(path, [(2, 4)]), tmp_path)
    assert data.startswith(id3v23(TITLE))
    assert b"CHAP" not in data[:200]


def test_a_tag_that_cant_be_rewritten_is_left_out():
    tag = id3v23(TITLE, CHAPTER)
    unsynced = tag[:5] + b"\x80" + tag[6:]
    assert without_chapters(unsynced) == b""
    assert without_chapters(id3v23(TITLE)) is not None
    plain = id3v23(TITLE)
    assert without_chapters(plain) is plain


def test_junk_between_frames_and_trailing_tags(tmp_path):
    src = make_mp3(tmp_path / "src.mp3")
    v1 = b"TAG" + b"\0" * 125
    path = with_tag(
        src, tmp_path / "a.mp3", id3v23(TITLE), junk=b"\x00\xff\xfb junk", tail=v1
    )
    layout = scan(path)
    assert layout.frames == scan(src).frames
    assert layout.end == path.stat().st_size - 128
    data = served(plan(path, [(5, 6)]), tmp_path)
    assert data.endswith(v1) and b"junk" not in data
    assert decoded(data)[0] == pytest.approx(SECONDS - 1, abs=0.1)


def test_not_an_mp3(tmp_path):
    (tmp_path / "a.mp3").write_bytes(b"<html>nope</html>" * 100)
    with pytest.raises(Mp3Error):
        plan(tmp_path / "a.mp3", [(1, 2)])
    (tmp_path / "b.mp3").write_bytes(b"")
    with pytest.raises(Mp3Error):
        scan(tmp_path / "b.mp3")


def test_manifest_round_trips(tmp_path):
    path = make_mp3(tmp_path / "a.mp3")
    m = plan(path, [(1, 2)])
    again = Manifest.from_json(m.to_json())
    assert (again.parts, again.removed, again.seconds, again.size, again.etag) == (
        m.parts,
        m.removed,
        m.seconds,
        m.size,
        m.etag,
    )
    assert m.files() == {"a.mp3"}
    assert whole("x.m4a", 10, "audio/mp4").parts == [["file", "x.m4a", 0, 10]]
