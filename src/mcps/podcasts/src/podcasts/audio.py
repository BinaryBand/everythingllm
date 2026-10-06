"""Episodes as downloaded, what to leave out of each, and what to serve.

Three folders in the state directory:
  audio/<sha256>.<ext>        each episode as downloaded, never changed; named by its hash,
                              so caches keyed by it (fingerprints, transcripts) stay right
                              however the cuts change
  cuts/<sha256>.json          the sidecar: {audio, cuts: [{start, end, source, reason,
                              active}], legacy_cut?}, times in seconds of the original.
                              Sources: "repeat" (scrub.py), "ad-read" (transcripts.py),
                              "agent". Only active cuts are left out; inactive ones are
                              reported (ad reads with ad_words "report").
  manifests/<slug>/<name>.json  what splice-web serves at /podcasts/<slug>/<name>

`render` turns the sidecar into the manifest. An MP3 is served as ranges of the original
(splice.plan); anything else with cuts, or an MP3 splice can't read, is cut once with
PyAV into audio/<sha256>.<cutid>.<ext>. The served name carries a short hash of the
active cuts (`<stem>.<cutid>.<ext>`; just `<stem>.<ext>` with none), so a podcast app
sees a new file whenever they change.
An episode downloaded before originals were kept has its cut file as the original, with
that cut recorded as `legacy_cut`.
"""

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from hostrpc import atomic_write
from splice.mp3 import Mp3Error
from splice.plan import Manifest, plan, whole

from podcasts.avio import AvCutter, AvDecoder, DecodeError
from podcasts.files import _read_json, _write_json

MP3 = "audio/mpeg"
GRACE = 24 * 3600  # seconds an unreferenced original or replaced manifest is kept


def cutid(audio: str, spans) -> str:
    """A short hash of the active cuts of `audio`; "" for none."""
    if not spans:
        return ""
    text = json.dumps([audio, [[round(a, 3), round(b, 3)] for a, b in spans]])
    return hashlib.sha256(text.encode()).hexdigest()[:10]


def named(stem: str, audio: str, spans) -> str:
    """`<stem>.<cutid>.<ext>` for `audio` with `spans` cut, or `<stem>.<ext>` with none."""
    cid = cutid(audio, spans)
    return f"{stem}.{cid}{Path(audio).suffix}" if cid else f"{stem}{Path(audio).suffix}"


def active(sidecar: dict) -> list[tuple[float, float]]:
    """The (start, end) of the cuts in `sidecar` that are left out."""
    return sorted(
        (c["start"], c["end"]) for c in sidecar.get("cuts", []) if c.get("active", True)
    )


def media_seconds(path: Path) -> float | None:
    try:
        return AvDecoder().duration(path) or None
    except (DecodeError, OSError):
        return None


@dataclass
class Rendered:
    served: str  # the name it is served under
    manifest: Manifest
    sidecar: dict  # as rendered


class AudioStore:
    def __init__(self, state: Path | str):
        state = Path(state)
        self.dir = state / "audio"
        self.cuts_dir = state / "cuts"
        self.manifests = state / "manifests"
        self.cutter = AvCutter()

    def path(self, audio: str) -> Path:
        return self.dir / audio

    # --- originals -------------------------------------------------------------------

    def temp(self, stem: str, ext: str) -> Path:
        """Where to download a file before `add` (a dot file, cleaned up by gc)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        return self.dir / f".dl-{stem}.{ext}"

    def add(self, file: Path, ext: str) -> str:
        """Move `file` in as an original; its name in the store (`<sha256>.<ext>`). A file
        already there (the same bytes) is kept, and `file` dropped."""
        with open(file, "rb") as f:
            name = f"{hashlib.file_digest(f, 'sha256').hexdigest()}.{ext}"
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.path(name).exists():
            file.unlink()
        else:
            os.chmod(file, 0o644)
            os.replace(file, self.path(name))
        if self.sidecar(name) is None:
            self.save_sidecar(name, _blank(name))
        return name

    # --- sidecars ----------------------------------------------------------------------

    def sidecar_path(self, audio: str) -> Path:
        return self.cuts_dir / f"{Path(audio).stem}.json"

    def sidecar(self, audio: str) -> dict | None:
        return _read_json(self.sidecar_path(audio))

    def save_sidecar(self, audio: str, d: dict) -> None:
        self.cuts_dir.mkdir(parents=True, exist_ok=True)
        _write_json(self.sidecar_path(audio), d)

    def cuts(self, audio: str) -> list[dict]:
        return (self.sidecar(audio) or {}).get("cuts", [])

    def active(self, audio: str) -> list[tuple[float, float]]:
        return active(self.sidecar(audio) or {})

    def set_source(self, audio: str, source: str, spans, active: bool = True) -> None:
        """Replace the cuts from `source` with `spans`; other sources' cuts stay."""
        d = self.sidecar(audio) or _blank(audio)
        kept = [c for c in d["cuts"] if c.get("source") != source]
        new = [
            {
                "start": round(a, 3),
                "end": round(b, 3),
                "source": source,
                "reason": "",
                "active": active,
            }
            for a, b in spans
        ]
        d["cuts"] = sorted(kept + new, key=lambda c: c["start"])
        self.save_sidecar(audio, d)

    # --- what is served -------------------------------------------------------------------

    def manifest_path(self, slug: str, served: str) -> Path:
        return self.manifests / slug / f"{served}.json"

    def manifest(self, slug: str, served: str) -> Manifest | None:
        try:
            return Manifest.from_json(self.manifest_path(slug, served).read_text())
        except FileNotFoundError:
            return None

    def render(
        self, slug: str, audio: str, stem: str, type_: str, replacing: str = ""
    ) -> Rendered:
        """Write the manifest for `audio` as its sidecar says, served as `stem` plus the cut
        hash. Cheap to call again: an existing manifest is reused. A problem making it is
        kept as the manifest's note. `replacing`, the name served until now, is kept GRACE
        more if this one differs, for apps that fetched the feed before and downloads under
        way."""
        side = self.sidecar(audio) or _blank(audio)
        spans = active(side)
        served = named(stem, audio, spans)
        if (
            replacing
            and replacing != served
            and (old := self.manifest_path(slug, replacing)).is_file()
        ):
            os.utime(old)  # gc goes by age
        if (m := self.manifest(slug, served)) is None:
            m = self._make(audio, spans, type_ or MP3)
            path = self.manifest_path(slug, served)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, m.to_json())
        return Rendered(served, m, side)

    def _make(self, audio: str, spans, type_: str) -> Manifest:
        src = self.path(audio)
        note = ""
        if src.suffix == ".mp3":
            try:
                return plan(src, spans, MP3)
            except (Mp3Error, OSError) as e:
                # Uncut, it's served as it is anyway; only a cut needs it read.
                note = (
                    f"couldn't read it as an MP3 to cut it ({e}); cut by re-muxing instead"
                    if spans
                    else ""
                )
        try:
            m = self._whole_or_cut(audio, spans, type_)
        except (DecodeError, OSError) as e:
            return whole(
                audio,
                src.stat().st_size,
                type_,
                note=f"couldn't cut it ({e}); serving it as downloaded",
            )
        m.note = note
        return m

    def _whole_or_cut(self, audio: str, spans, type_: str) -> Manifest:
        """Not an MP3 splice can read: the file as it is, or with no spans, or cut once with
        PyAV into its own file."""
        src = self.path(audio)
        if not spans or type_.startswith("video/"):
            return whole(audio, src.stat().st_size, type_, media_seconds(src))
        name = named(Path(audio).stem, audio, spans)
        out = self.path(name)
        if not out.exists():
            tmp = self.dir / f".{name}"
            try:
                self.cutter.cut(src, tmp, spans)
                os.chmod(tmp, 0o644)
                os.replace(tmp, out)
            finally:
                tmp.unlink(missing_ok=True)
        return Manifest(
            type_,
            [["file", name, 0, out.stat().st_size]],
            [list(s) for s in spans],
            media_seconds(out),
        )

    # --- cleaning up -------------------------------------------------------------------------

    def gc(
        self, live: dict[str, set[str]], originals: set[str], grace: float = GRACE
    ) -> None:
        """Keep the manifests in `live` ({slug: served names}), the originals in `originals`,
        and what those manifests point to; delete the rest once `grace` seconds old (a
        manifest counts from when it was replaced), and the sidecars of originals gone."""
        now = time.time()
        referenced = self._gc_manifests(live, now - grace) | originals
        self._gc_audio(referenced, now - grace)
        self._gc_sidecars()

    def _gc_manifests(self, live: dict[str, set[str]], before: float) -> set[str]:
        """Delete stale manifests; the files the ones kept point to."""
        referenced: set[str] = set()
        for folder in self.manifests.glob("*/"):
            names = live.get(folder.name, set())
            for f in folder.glob("*.json"):
                if f.name.removesuffix(".json") not in names and _older(f, before):
                    f.unlink(missing_ok=True)
                    continue
                try:
                    referenced |= Manifest.from_json(f.read_text()).files()
                except (OSError, ValueError, KeyError, TypeError):
                    f.unlink(missing_ok=True)
            try:
                folder.rmdir()  # only if empty
            except OSError:
                pass
        return referenced

    def _gc_audio(self, referenced: set[str], before: float) -> None:
        for f in self.dir.glob("*") if self.dir.is_dir() else []:
            if f.name not in referenced and _older(f, before):
                f.unlink(missing_ok=True)

    def _gc_sidecars(self) -> None:
        """A sidecar goes with its original."""
        stems = (
            {f.name.split(".")[0] for f in self.dir.iterdir()}
            if self.dir.is_dir()
            else set()
        )
        for f in self.cuts_dir.glob("*.json"):
            if f.stem not in stems:
                f.unlink(missing_ok=True)


def _blank(audio: str) -> dict:
    return {"audio": audio, "cuts": []}


def _older(f: Path, before: float) -> bool:
    try:
        return f.stat().st_mtime < before
    except FileNotFoundError:
        return False
