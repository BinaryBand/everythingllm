"""Text to speech with Kokoro (kokoro-onnx), locally on the CPU, for the Daily News read
aloud. The `speak` command (podcasts.cli) runs it by hand.
"""

from __future__ import annotations

import hashlib
import re
import threading
import urllib.request
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Protocol

import av
import numpy as np
from numpy.typing import NDArray

# Typed failures raised while turning text into audio.


class SpeakError(Exception):
    """Base class for every error this module raises."""


class InvalidSpeechError(SpeakError):
    """The text, the voice, the speed or the output path cannot be used."""


class ModelMissingError(SpeakError):
    """The speech model files are not on disk."""


class ModelFetchError(SpeakError):
    """The speech model files could not be downloaded or did not match their checksum."""


class SynthesisError(SpeakError):
    """The engine could not turn the text into audio."""


# The Kokoro model files: where they come from, and how to fetch them.

_BASE_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
)
_CHUNK = 1 << 20
_TIMEOUT = 60  # seconds without a byte before a download is abandoned

Opener = Callable[[str], AbstractContextManager[IO[bytes]]]


@dataclass(frozen=True, slots=True)
class ModelFile:
    name: str
    sha256: str


MODEL = ModelFile(
    "kokoro-v1.0.onnx",
    "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
)
VOICES = ModelFile(
    "voices-v1.0.bin",
    "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
)


def _open(url: str) -> AbstractContextManager[IO[bytes]]:
    return urllib.request.urlopen(url, timeout=_TIMEOUT)


def missing(dest: Path) -> list[ModelFile]:
    """The model files that are absent from `dest` or fail their checksum."""
    return [f for f in (MODEL, VOICES) if not _is_intact(dest / f.name, f)]


def download(file: ModelFile, dest: Path, opener: Opener = _open) -> None:
    """Fetch `file` into `dest`, under a temporary name until its checksum matches.

    `dest` never holds a partial file: a failed or corrupt download leaves nothing behind.
    """
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / file.name
    part = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    try:
        with opener(_BASE_URL + file.name) as resp, part.open("wb") as out:
            while chunk := resp.read(_CHUNK):
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != file.sha256:
            raise ModelFetchError(f"{file.name} does not match its expected checksum")
        part.replace(target)
    except OSError as exc:  # URLError and HTTPError are OSErrors
        raise ModelFetchError(f"could not download {file.name}: {exc}") from exc
    finally:
        part.unlink(missing_ok=True)


def _is_intact(path: Path, file: ModelFile) -> bool:
    if not path.is_file():
        return False
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest() == file.sha256


# Request and audio value objects.

DEFAULT_VOICE = "af_heart"
DEFAULT_LANG = "en-us"
DEFAULT_SPEED = 1.0


@dataclass(frozen=True, slots=True)
class SpeechRequest:
    """Text to speak. `speed` scales the pace (1.0 is normal).

    `lang` picks the phonemizer; left unset, the synthesizer takes it from the voice.

    `voice` is a voice name, or a weighted blend such as `af_heart:0.6,af_nicole:0.4`.
    With `phonemes` set, `text` is IPA in Kokoro's alphabet and is spoken as given.
    At the top level `text` may carry the markup described above `parse`; a synthesizer
    only ever sees plain text or phonemes.
    """

    text: str
    voice: str = DEFAULT_VOICE
    speed: float = DEFAULT_SPEED
    lang: str | None = None
    phonemes: bool = False


@dataclass(frozen=True, slots=True)
class Pause:
    """Silence between utterances."""

    seconds: float


@dataclass(frozen=True, slots=True)
class Audio:
    """Mono, 16-bit little-endian PCM samples."""

    pcm: bytes
    sample_rate: int

    @property
    def seconds(self) -> float:
        return len(self.pcm) / 2 / self.sample_rate  # two bytes a sample


# `Synthesizer` backed by Kokoro, run locally on the CPU through kokoro-onnx.

# Kokoro names a voice after its language (first letter) and gender (second).
_LANGUAGES = {
    "a": "en-us",
    "b": "en-gb",
    "e": "es",
    "f": "fr-fr",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt-br",
    "z": "cmn",
}


class Engine(Protocol):
    """The slice of `kokoro_onnx.Kokoro` this adapter uses."""

    def get_voices(self) -> list[str]: ...

    def get_voice_style(self, name: str) -> NDArray[np.float32]: ...

    def create(
        self,
        text: str,
        voice: str | NDArray[np.float32],
        speed: float,
        lang: str,
        is_phonemes: bool,  # noqa: V107 - vulture does not count keyword arguments as uses
    ) -> tuple[NDArray[np.float32], int]: ...


Loader = Callable[[Path, Path], Engine]


def _load_kokoro(model: Path, voices: Path) -> Engine:
    from kokoro_onnx import (
        Kokoro,
    )

    return Kokoro(str(model), str(voices))


def to_pcm16(samples: NDArray[np.float32]) -> bytes:
    """Float samples in [-1, 1] as little-endian signed 16-bit PCM. Louder values are clipped."""
    return (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def _voice(engine: Engine, spec: str) -> str | NDArray[np.float32]:
    """A voice name, or a `name[:weight],...` blend as the weighted mean of the voices' styles."""
    known = engine.get_voices()
    blend: list[tuple[str, float]] = []
    for part in spec.split(","):
        name, _, raw = part.strip().partition(":")
        try:
            weight = float(raw) if raw else 1.0
        except ValueError:
            weight = 0.0
        if name not in known:
            raise InvalidSpeechError(
                f"unknown voice {name!r}; available: {', '.join(known)}"
            )
        if weight <= 0:
            raise InvalidSpeechError(
                f"bad voice blend {spec!r}: weights must be positive numbers"
            )
        blend.append((name, weight))
    if len(blend) == 1 and ":" not in spec:
        return spec.strip()
    total = sum(weight for _, weight in blend)
    styles = [engine.get_voice_style(name) * (weight / total) for name, weight in blend]
    return np.sum(styles, axis=0, dtype=np.float32)


def _language(spec: str) -> str:
    """The language of a voice name, or of the first voice of a blend."""
    return _LANGUAGES.get(spec.strip()[:1], DEFAULT_LANG)


class KokoroSynthesizer:
    """Loads the model on first use and keeps it for later calls.

    With `on_download`, a missing or corrupt model is downloaded then, and `on_download`
    hears about each file first; without it, that is a `ModelMissingError`.

    Calls from several threads take turns: the engine is loaded once and is not known to be
    reentrant.
    """

    def __init__(
        self,
        model_dir: Path,
        load: Loader = _load_kokoro,
        on_download: Callable[[str], None] | None = None,
    ) -> None:
        self._dir = model_dir
        self._load = load
        self._on_download = on_download
        self._engine: Engine | None = None
        self._lock = threading.Lock()

    def synthesize(self, req: SpeechRequest) -> Audio:
        with self._lock:
            engine = self._engine or self._start()
            voice = _voice(engine, req.voice)
            try:
                samples, rate = engine.create(
                    req.text,
                    voice=voice,
                    speed=req.speed,
                    lang=req.lang or _language(req.voice),
                    is_phonemes=req.phonemes,
                )
            except (
                Exception
            ) as exc:  # the engine wraps onnxruntime and espeak; anything can surface
                raise SynthesisError(f"kokoro failed: {exc}") from exc
        return Audio(to_pcm16(samples), rate)

    def _start(self) -> Engine:
        model, voices = self._dir / MODEL.name, self._dir / VOICES.name
        if todo := missing(self._dir):  # absent, or a corrupt or partial file
            if self._on_download is None:
                raise ModelMissingError(
                    f"speech model not found in {self._dir}; run: speak fetch"
                )
            for file in todo:
                self._on_download(f"downloading {file.name}")
                download(file, self._dir)
        try:
            self._engine = self._load(model, voices)
        except Exception as exc:  # a corrupt or incompatible model file
            raise SynthesisError(
                f"could not load the model from {self._dir}: {exc}"
            ) from exc
        return self._engine


# MP3 encoding through PyAV's LAME.

BIT_RATE = 64_000  # plenty for one voice
_FRAME = 1152  # samples per MP3 frame


class Mp3Store:
    """Writes `path` whole, through a temporary file beside it, so a reader never sees half."""

    def write(self, audio: Audio, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(f".{path.name}.part")
        samples = np.frombuffer(audio.pcm, dtype="<i2")
        try:
            with av.open(str(part), "w", format="mp3") as out:
                stream = out.add_stream(
                    "libmp3lame", rate=audio.sample_rate, layout="mono"
                )
                stream.bit_rate = BIT_RATE
                for start in range(0, len(samples), _FRAME):
                    frame = av.AudioFrame.from_ndarray(
                        samples[None, start : start + _FRAME],
                        format="s16",
                        layout="mono",
                    )
                    frame.sample_rate = audio.sample_rate
                    for pkt in stream.encode(frame):
                        out.mux(pkt)
                for pkt in stream.encode(None):
                    out.mux(pkt)
            part.chmod(0o644)
            part.replace(path)
        finally:
            part.unlink(missing_ok=True)


# The markup a request's text may carry: a small SSML subset for fine control.
#
# - `<break time="500ms"/>` or `time="1.5s"`: a pause of up to 10 seconds.
# - `<phoneme ph="IPA">word</phoneme>`: speak the IPA, not the word. Put the primary (U+02C8)
#   or secondary (U+02CC) stress mark before a stressed syllable. The word between the tags
#   is a fallback and is never spoken.
# - `<voice name="bm_george">...</voice>`: speak the span in another voice, or a blend such as
#   `name="af_heart:0.6,af_nicole:0.4"`.
# - `<prosody rate="1.2">...</prosody>`: scale the pace of the span; `rate="80%"` also works.
#   Nested rates multiply.
#
# `&lt;`, `&gt;` and `&amp;` write a literal `<`, `>` and `&`. Any other `<...>` is plain text.
# A known tag that is malformed, unclosed or misnested raises `InvalidSpeechError`.

MAX_PAUSE = 10.0  # seconds

_TAG = re.compile(r"<(/?)(break|phoneme|voice|prosody)\b([^<>]*?)(/?)>")
_ATTR = re.compile(r"""\s+([a-z]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_ENTITY = re.compile(r"&(lt|gt|amp);")
_ENTITIES = {"lt": "<", "gt": ">", "amp": "&"}
_TIME = re.compile(r"(\d+(?:\.\d+)?)(ms|s)")
_RATE = re.compile(r"(\d+(?:\.\d+)?)(%?)")
_ATTRIBUTE = {"break": "time", "phoneme": "ph", "voice": "name", "prosody": "rate"}

Part = SpeechRequest | Pause


def parse(req: SpeechRequest) -> list[Part]:
    """Split `req.text` into utterances and pauses, each utterance carrying its own settings."""
    return _Parser(req).run()


class _Parser:
    def __init__(self, base: SpeechRequest) -> None:
        self._text = base.text
        self._style = base  # the voice, speed and lang in force at this point
        self._open: list[
            tuple[str, SpeechRequest]
        ] = []  # tag, and the style to restore
        self._ipa: str | None = None  # set while inside <phoneme>
        self._parts: list[Part] = []

    def run(self) -> list[Part]:
        pos = 0
        for tag in _TAG.finditer(self._text):
            self._words(self._text[pos : tag.start()])
            self._tag(*tag.groups())
            pos = tag.end()
        self._words(self._text[pos:])
        if self._open:
            raise InvalidSpeechError(f"<{self._open[-1][0]}> is never closed")
        if not any(isinstance(p, SpeechRequest) for p in self._parts):
            raise InvalidSpeechError("nothing to say: the text has no words to speak")
        return self._parts

    def _words(self, raw: str) -> None:
        text = _ENTITY.sub(lambda m: _ENTITIES[m[1]], raw).strip()
        if text and self._ipa is None:
            self._parts.append(replace(self._style, text=text))

    def _tag(self, closing: str, name: str, attrs: str, selfclosing: str) -> None:
        if self._ipa is not None and not (closing and name == "phoneme"):
            raise InvalidSpeechError(f"<{name}> cannot appear inside <phoneme>")
        if closing:
            self._close(name, attrs, selfclosing)
        elif name == "break":
            if not selfclosing:
                raise InvalidSpeechError(
                    '<break> has no content; write <break time="500ms"/>'
                )
            self._parts.append(Pause(_seconds(_attribute(name, attrs))))
        elif selfclosing:
            raise InvalidSpeechError(
                f"<{name}/> needs content; write <{name} ...>...</{name}>"
            )
        else:
            self._start(name, _attribute(name, attrs))

    def _start(self, name: str, value: str) -> None:
        self._open.append((name, self._style))
        if name == "phoneme":
            self._ipa = value
        elif name == "voice":
            self._style = replace(self._style, voice=value)
        else:
            self._style = replace(self._style, speed=self._style.speed * _rate(value))

    def _close(self, name: str, attrs: str, selfclosing: str) -> None:
        if attrs.strip() or selfclosing or name == "break":
            raise InvalidSpeechError(f"</{name}> is not a valid closing tag")
        if not self._open or self._open[-1][0] != name:
            raise InvalidSpeechError(f"</{name}> closes nothing that is open")
        _, self._style = self._open.pop()
        if name == "phoneme":
            ipa, self._ipa = self._ipa or "", None
            if ipa.strip():
                self._parts.append(
                    replace(self._style, text=ipa.strip(), phonemes=True)
                )


def _attribute(name: str, attrs: str) -> str:
    """The value of `name`'s one attribute; anything else is an error."""
    key = _ATTRIBUTE[name]
    found = {m[1]: m[2] if m[2] is not None else m[3] for m in _ATTR.finditer(attrs)}
    if _ATTR.sub("", attrs).strip() or list(found) != [key]:
        raise InvalidSpeechError(f'<{name}> takes exactly one attribute, {key}="..."')
    return found[key]


def _seconds(value: str) -> float:
    m = _TIME.fullmatch(value.strip())
    if not m:
        raise InvalidSpeechError(
            f'<break> time must look like "500ms" or "1.5s", got {value!r}'
        )
    seconds = float(m[1]) / (1000 if m[2] == "ms" else 1)
    if seconds > MAX_PAUSE:
        raise InvalidSpeechError(
            f"<break> time is at most {MAX_PAUSE:g}s, got {value!r}"
        )
    return seconds


def _rate(value: str) -> float:
    m = _RATE.fullmatch(value.strip())
    factor = float(m[1]) / (100 if m[2] else 1) if m else 0.0
    if factor <= 0:
        raise InvalidSpeechError(
            f'<prosody> rate must be a positive number like "1.2" or "80%", got {value!r}'
        )
    return factor


class Synthesizer(Protocol):
    """Turns text into audio."""

    def synthesize(self, req: SpeechRequest) -> Audio:
        """Speak one utterance. Every call returns the same sample rate.

        Raises `ModelMissingError` or `SynthesisError`.
        """
        ...


_SAMPLE = b"\x00\x00"  # one 16-bit sample of silence


def synthesize(req: SpeechRequest, synth: Synthesizer) -> Audio:
    """The spoken `req.text`, which may carry the markup described above `parse`."""
    if not req.text.strip():
        raise InvalidSpeechError("nothing to say: the text is empty")
    if req.speed <= 0:
        raise InvalidSpeechError(f"speed must be positive, got {req.speed}")
    return _render(parse(req), synth)


def _render(parts: list[SpeechRequest | Pause], synth: Synthesizer) -> Audio:
    """Synthesize each utterance and join the results, with silence for each pause."""
    spoken = [synth.synthesize(p) if isinstance(p, SpeechRequest) else p for p in parts]
    rate = next(
        a.sample_rate for a in spoken if isinstance(a, Audio)
    )  # parse keeps at least one
    pcm = b"".join(
        a.pcm if isinstance(a, Audio) else _SAMPLE * round(a.seconds * rate)
        for a in spoken
    )
    return Audio(pcm, rate)
