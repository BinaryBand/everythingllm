import av
import pytest
from podcasts.speech import (
    Audio,
    InvalidSpeechError,
    Mp3Store,
    Pause,
    SpeechRequest,
    parse,
    synthesize,
)


class Synth:
    def synthesize(self, req):
        return Audio(b"\x01\x00" * 24000, 24000)  # one second


def test_markup_splits_speech_and_pauses():
    parts = parse(
        SpeechRequest(
            'Hello. <break time="500ms"/> <prosody rate="1.5">fast &amp; loud</prosody>'
        )
    )
    assert parts == [
        SpeechRequest("Hello."),
        Pause(0.5),
        SpeechRequest("fast & loud", speed=1.5),
    ]


def test_markup_rejects_unclosed_tags():
    with pytest.raises(InvalidSpeechError):
        parse(SpeechRequest('<voice name="bm_george">hi'))


def test_synthesize_joins_with_silence():
    audio = synthesize(SpeechRequest('a <break time="1s"/> b'), Synth())
    assert len(audio.pcm) == 3 * 24000 * 2


def test_mp3_store_writes_a_playable_file(tmp_path):
    path = tmp_path / "out" / "news.mp3"
    Mp3Store().write(Audio(b"\x00\x10" * 24000 * 3, 24000), path)
    with av.open(str(path)) as f:
        assert f.duration is not None
        assert f.duration / av.time_base == pytest.approx(3, abs=0.1)
        assert f.streams.audio[0].sample_rate == 24000
    assert sorted(p.name for p in path.parent.iterdir()) == ["news.mp3"]
    assert oct(path.stat().st_mode & 0o777) == "0o644"
