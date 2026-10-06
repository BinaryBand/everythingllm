"""The by-hand commands over the podcasts' audio code, apart from the modules the services
import:
  spot repeats AUDIO...   the stretches recordings share, such as ads (podcasts.fingerprint)
  transcribe AUDIO        speech to text with Whisper (podcasts.whisper)
  speak say / speak fetch text to speech with Kokoro (podcasts.speech)

The models are kept where the services keep theirs (podcasts.library.models_dir), so a
`speak fetch` or a first `transcribe` downloads them for the services too.

Config (environment):
  PODCASTS_MODELS     where the models are kept (default ~/.local/share/everythingllm/models)
  ANYTHINGLLM_STORAGE AnythingLLM's storage directory (via hostrpc)
"""

import io
import json
import sys
import wave
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from podcasts.avio import AvDecoder, DecodeError
from podcasts.fingerprint import (
    DEFAULT_MIN_LENGTH,
    FingerprintError,
    Recording,
    RepeatsRequest,
    repeats,
)
from podcasts.rss import clock
from podcasts.segments import TranscriptFormat, render_transcript
from podcasts.speech import (
    DEFAULT_SPEED,
    DEFAULT_VOICE,
    Audio,
    InvalidSpeechError,
    KokoroSynthesizer,
    SpeakError,
    SpeechRequest,
    download,
    missing,
    synthesize,
)
from podcasts.whisper import (
    DEFAULT_MODEL,
    InvalidTranscriptionError,
    ModelSize,
    TranscribeError,
    TranscriptionRequest,
    WhisperTranscriber,
    transcribe,
)


def _models(name: str) -> Path:
    from podcasts.library import models_dir  # only when it's needed: it loads httpx

    return models_dir(name)


def _main(
    app: typer.Typer,
    prog: str,
    errors: tuple[type[Exception], ...],
    argv: list[str] | None,
) -> int:
    try:
        app(args=argv, prog_name=prog)
    except SystemExit as exc:  # Typer always exits
        return int(exc.code or 0)
    except errors as exc:
        _note(prog, str(exc))
        return 1
    return 0


def _note(prog: str, message: str) -> None:
    print(f"{prog}: {message}", file=sys.stderr)


def _app(help_: str) -> typer.Typer:
    return typer.Typer(
        add_completion=False,
        pretty_exceptions_enable=False,
        rich_markup_mode=None,
        help=help_,
    )


# `spot repeats`: the stretches of audio that recordings share.


class RepeatsFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


spot_app = _app("Find what audio recordings share, such as ads.")


@spot_app.command("repeats")
def find_repeats(
    audio: Annotated[
        list[Path], typer.Argument(help="two or more recordings to compare")
    ],
    *,
    min_length: Annotated[
        float, typer.Option(help="shortest shared stretch to report, in seconds")
    ] = DEFAULT_MIN_LENGTH,
    fmt: Annotated[RepeatsFormat, typer.Option("--format")] = RepeatsFormat.TEXT,
) -> None:
    """Print each recording's stretches of audio that also play in another, such as ads."""
    found = repeats(RepeatsRequest(tuple(audio), min_length), AvDecoder())
    print(render_repeats(found, fmt), end="")


@spot_app.callback()
def _spot() -> None:
    # Keeps `repeats` a subcommand; with one command, typer makes it the whole CLI.
    pass


def render_repeats(recordings: list[Recording], fmt: RepeatsFormat) -> str:
    """Each recording and its repeats, one per line in text, else a JSON array."""
    match fmt:
        case RepeatsFormat.TEXT:
            return "".join(
                f"{rec.audio}\n"
                + "".join(
                    f"  {_centis(r.start)}  {_centis(r.end)}  also in {', '.join(map(str, r.also))}\n"
                    for r in rec.repeats
                )
                for rec in recordings
            )
        case RepeatsFormat.JSON:
            rows = [
                {
                    "audio": str(rec.audio),
                    "repeats": [
                        {
                            "start": r.start,
                            "end": r.end,
                            "also": [str(p) for p in r.also],
                        }
                        for r in rec.repeats
                    ],
                }
                for rec in recordings
            ]
            return json.dumps(rows, indent=2) + "\n"


def _centis(seconds: float) -> str:
    """`H:MM:SS.ss`, or `M:SS.ss` under an hour."""
    centis = round(seconds * 100)
    minutes, centis = divmod(centis, 6000)
    hours, minutes = divmod(minutes, 60)
    secs = f"{centis // 100:02}.{centis % 100:02}"
    return f"{hours}:{minutes:02}:{secs}" if hours else f"{minutes}:{secs}"


def spot_main(argv: list[str] | None = None) -> int:
    return _main(spot_app, "spot", (FingerprintError, DecodeError), argv)


# `transcribe AUDIO`: speech to text.

transcribe_app = _app("Turn speech into text.")


@transcribe_app.command()
def run(
    audio: Annotated[
        Path, typer.Argument(help="an audio or video file (mp3, m4a, wav, mp4, ...)")
    ],
    *,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="write here; default stdout")
    ] = None,
    model: Annotated[
        ModelSize, typer.Option(help="base is fastest, medium most accurate")
    ] = DEFAULT_MODEL,
    fmt: Annotated[TranscriptFormat, typer.Option("--format")] = TranscriptFormat.TEXT,
    lang: Annotated[
        str | None, typer.Option(help="language code such as en; default detected")
    ] = None,
    prompt: Annotated[
        str | None, typer.Option(help="names and terms to steer the spelling")
    ] = None,
    model_dir: Annotated[
        Path,
        typer.Option(
            default_factory=lambda: _models("whisper"),
            show_default="<storage>/models/whisper",
        ),
    ],
) -> None:
    """Transcribe AUDIO. A model that is not on disk is downloaded first."""
    req = TranscriptionRequest(audio, model=model, language=lang, prompt=prompt)
    if (
        output is not None and not output.parent.is_dir()
    ):  # before minutes of work, not after
        raise InvalidTranscriptionError(f"no such directory: {output.parent}")
    progress = _progress if sys.stderr.isatty() else None
    transcriber = WhisperTranscriber(model_dir, lambda msg: _note("transcribe", msg))
    found = transcribe(req, transcriber, progress)
    if progress:
        print(file=sys.stderr)
    text = render_transcript(found, fmt)
    if output is None:
        print(text, end="")
        return
    try:
        output.write_text(text)
    except OSError as exc:
        raise InvalidTranscriptionError(f"cannot write {output}: {exc}") from exc
    print(output)


def _progress(done: float, total: float) -> None:
    print(
        f"\rtranscribe: {clock(done)} of {clock(total)}",
        end="",
        file=sys.stderr,
        flush=True,
    )


def transcribe_main(argv: list[str] | None = None) -> int:
    return _main(transcribe_app, "transcribe", (TranscribeError,), argv)


# `speak say` and `speak fetch`: text to speech, written to a WAV file.

speak_app = _app("Turn text into speech.")

ModelDir = Annotated[
    Path,
    typer.Option(
        default_factory=lambda: _models("kokoro"),
        show_default="<storage>/models/kokoro",
    ),
]

_MONO = 1
_SAMPLE_WIDTH = 2  # bytes: 16-bit PCM


@speak_app.command()
def say(
    text: Annotated[
        str | None, typer.Argument(help="text; default --file, else stdin")
    ] = None,
    *,
    output: Annotated[Path, typer.Option("--output", "-o", metavar="OUT.wav")],
    file: Annotated[
        Path | None, typer.Option(help="read the text from this file")
    ] = None,
    voice: Annotated[
        str, typer.Option(help="voice or blend, e.g. a:0.6,b:0.4")
    ] = DEFAULT_VOICE,
    speed: Annotated[float, typer.Option(help="pace, 1.0 is normal")] = DEFAULT_SPEED,
    lang: Annotated[
        str | None, typer.Option(help="phonemizer language; default from the voice")
    ] = None,
    model_dir: ModelDir,
) -> None:
    """Write text to a WAV file and print its path.

    The text may use the tags <break>, <phoneme>, <voice> and <prosody> for fine control;
    see the README.
    """
    if output.suffix.lower() != ".wav":
        raise InvalidSpeechError(f"output must be a .wav file, got {output.name!r}")
    req = SpeechRequest(_text(text, file), voice=voice, speed=speed, lang=lang)
    audio = synthesize(req, KokoroSynthesizer(model_dir))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(encode_wav(audio))
    print(output)


@speak_app.command()
def fetch(model_dir: ModelDir) -> None:
    """Download the speech model (about 350 MB); does nothing when it is already there."""
    todo = missing(model_dir)
    for file in todo:
        _note("speak", f"downloading {file.name}")
        download(file, model_dir)
    if not todo:
        _note("speak", f"model already in {model_dir}")


def encode_wav(audio: Audio) -> bytes:
    """The audio as the bytes of a WAV file."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(_MONO)
        out.setsampwidth(_SAMPLE_WIDTH)
        out.setframerate(audio.sample_rate)
        out.writeframes(audio.pcm)
    return buffer.getvalue()


def _text(text: str | None, file: Path | None) -> str:
    if text is not None and file is not None:
        raise InvalidSpeechError("give the text or --file, not both")
    if file is not None:
        try:
            return file.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            raise InvalidSpeechError(f"cannot read {file}: {exc}") from exc
    if text is not None:
        return text
    if sys.stdin.isatty():
        _note("speak", "reading the text from stdin; end it with Ctrl-D")
    return sys.stdin.read()


def speak_main(argv: list[str] | None = None) -> int:
    return _main(speak_app, "speak", (SpeakError,), argv)
