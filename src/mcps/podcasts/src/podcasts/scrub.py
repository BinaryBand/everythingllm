"""Ad scrubbing: cut out the audio an episode shares with the show's other episodes.

Ads, and the bumpers around ad breaks, play in many episodes of a show; the talk plays in
one. podcasts.fingerprint finds the stretches of at least 4 s that an episode shares with any other; the
library notes them as the episode's "repeat" cuts (audio.py), which are left out of what
is served. The show's theme goes too where it plays alone, since it repeats just the same.
A repeat longer than MAX_REPEAT is kept: that is a rerun or a replayed segment, not an ad.

Each episode's fingerprint (the spectral peaks of the original, packed 8 bytes a frame: about
2 MB an hour) is kept in state/prints/<slug>/<key>.npy, the key being the original's hash,
so a new episode is compared against earlier ones without decoding them again, including
ones pruned. The newest PEERS fingerprints of a show are kept.
"""

import io
import shutil
from pathlib import Path

import numpy as np
from hostrpc import atomic_write

from podcasts.avio import AvDecoder, DecodeError
from podcasts.fingerprint import RATE, log_mel, peaks, shared_with

PEERS = 10  # fingerprints kept per show, and compared against
MAX_REPEAT = 8 * 60  # seconds
_BANDS = 64  # fingerprint's peak map width, packed into 8 bytes


class ScrubError(Exception):
    """An episode couldn't be read; the message goes into the feed's errors."""


class Scrubber:
    def __init__(self, prints: Path | str):
        self.prints = Path(prints)
        self.decoder = AvDecoder()

    def _dir(self, slug: str) -> Path:
        return self.prints / slug

    def fingerprint(self, slug: str, key: str, audio: Path) -> np.ndarray:
        """The peaks of `audio`, the original with its ads, from the cache if they were
        taken before."""
        file = self._dir(slug) / f"{key}.npy"
        try:
            return _unpack(np.load(file))
        except (FileNotFoundError, ValueError):
            pass
        try:
            found = peaks(log_mel(self.decoder.decode(audio, RATE)))
        except DecodeError as e:
            raise ScrubError(str(e)) from None
        file.parent.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        np.save(buf, np.packbits(found, axis=1))
        atomic_write(file, buf.getvalue())
        return found

    def _peers(self, slug: str, key: str) -> list[np.ndarray]:
        own = f"{key}.npy"
        files = [f for f in self._newest(slug) if f.name != own][:PEERS]
        out = []
        for f in files:
            try:
                out.append(_unpack(np.load(f)))
            except (
                ValueError
            ):  # cut short by a crash; it is taken again if still needed
                f.unlink(missing_ok=True)
        return out

    def scrub(
        self, slug: str, key: str, audio: Path
    ) -> list[tuple[float, float]] | None:
        """The (start, end) seconds `audio` shares with the show's other episodes, or None
        when there is no other episode to compare with yet."""
        target = self.fingerprint(slug, key, audio)
        peers = self._peers(slug, key)
        if not peers:
            return None
        return [
            (r.start, r.end)
            for r in shared_with(target, peers)
            if r.end - r.start <= MAX_REPEAT
        ]

    def prune(self, slug: str, keys: set[str]) -> None:
        """Keep the fingerprints of `keys` and the newest others, up to PEERS in all."""
        wanted = {f"{k}.npy" for k in keys}
        room = PEERS - len(wanted)
        for f in self._newest(slug):
            if f.name in wanted:
                continue
            if room > 0:
                room -= 1
            else:
                f.unlink(missing_ok=True)

    def forget(self, slug: str) -> None:
        shutil.rmtree(self._dir(slug), ignore_errors=True)

    def _newest(self, slug: str) -> list[Path]:
        try:
            files = [
                f
                for f in self._dir(slug).iterdir()
                if f.suffix == ".npy" and not f.name.startswith(".")
            ]
        except FileNotFoundError:
            return []
        return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)


def _unpack(packed: np.ndarray) -> np.ndarray:
    return np.unpackbits(packed, axis=1, count=_BANDS).astype(bool)
