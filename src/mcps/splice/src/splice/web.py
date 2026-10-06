"""`splice-web`: the podcasts on the pages site, episodes put together as they are fetched.

`tailscale serve` maps https://<host>:8445/podcasts/ to this server, ahead of the pages
site's Caddy. For `<slug>/<name>` it looks for `<manifests>/<slug>/<name>.json` (see
plan.py) and serves what it describes: ranges of a file in the audio folder, which is
never changed, and a few bytes of its own. Anything else (feeds, transcripts, the index,
an episode without a manifest) is served as a file from the root, like Caddy would, with
the pages site's headers. Range requests work for both, so podcast apps can seek and
resume.

Config (command line, defaults under ~/.local/share/everythingllm):
  --root       the served folder      (site/podcasts)
  --manifests  manifests by slug      (podcasts/manifests)
  --audio      what manifests point to (podcasts/audio)
"""

import argparse
import base64
import mimetypes
import os
import re
import stat
from email.utils import formatdate, parsedate_to_datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from splice.plan import Manifest

NAME_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,200}")
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
TYPES = {
    ".xml": "text/xml; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".vtt": "text/vtt; charset=utf-8",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
}
# As the pages site's Caddy sends them to what isn't a page (host/caddy/pages.Caddyfile;
# src/mcps/sites/tests/test_host_files.py checks they match).
CSP = "default-src 'self'; script-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'"


MANIFESTS_CACHED = 256


class NotFound(Exception):
    pass


class Unsatisfiable(Exception):
    """The range asked for lies past the end."""


def byte_range(header: str, size: int) -> tuple[int, int] | None:
    """The [start, end] (inclusive) that a Range header asks for; None to send it all (more
    than one range, or one this can't read). Raises Unsatisfiable if it lies past the end."""
    m = RANGE_RE.fullmatch(header.strip())
    if not m or not (m[1] or m[2]) or (m[1] and m[2] and int(m[2]) < int(m[1])):
        return None
    if not m[1]:  # the last n bytes
        n = int(m[2])
        start, end = size - n, size - 1
        if not n or not size:
            raise Unsatisfiable
        return max(0, start), end
    start = int(m[1])
    if start >= size:
        raise Unsatisfiable
    return start, min(int(m[2]), size - 1) if m[2] else size - 1


class Handler(BaseHTTPRequestHandler):
    protocol_version = (
        "HTTP/1.1"  # keep-alive, so seeking doesn't open a connection each time
    )
    timeout = 120  # seconds an idle connection is kept
    root: Path
    manifests: Path
    audio: Path
    prefix: str  # where tailscale serve mounts this server, e.g. /podcasts
    # Seeking asks for the same manifest again and again: {file: (mtime_ns, manifest, parts)}.
    cache: dict[Path, tuple[int, Manifest, list[tuple]]]

    @classmethod
    def configure(
        cls, root: Path, manifests: Path, audio: Path, prefix: str = "/podcasts"
    ) -> None:
        cls.root, cls.manifests, cls.audio, cls.prefix = (
            Path(root),
            Path(manifests),
            Path(audio),
            prefix.rstrip("/"),
        )
        cls.cache = {}

    def do_GET(self) -> None:
        self.respond(body=True)

    def do_HEAD(self) -> None:
        self.respond(body=False)

    def respond(self, body: bool) -> None:
        path = unquote(urlsplit(self.path).path)
        if path == "/health":
            return self.small(HTTPStatus.OK, b"ok", "text/plain; charset=utf-8", body)
        if path == self.prefix:
            return self.small(
                HTTPStatus.PERMANENT_REDIRECT,
                b"",
                "text/plain",
                body,
                {"Location": self.prefix + "/"},
            )
        # With or without the prefix, whether or not tailscale serve strips it.
        rel = path[len(self.prefix) :] if path.startswith(self.prefix + "/") else path
        names = rel.strip("/").split("/") if rel.strip("/") else []
        if rel.endswith("/"):
            names.append("index.html")
        try:
            if (
                not names
                or len(names) > 2
                or not all(NAME_RE.fullmatch(n) for n in names)
            ):
                raise NotFound
            if len(names) == 2 and (found := self.manifest(*names)):
                mtime, manifest, parts = found
                if any(p[0] == "file" and not p[1].is_file() for p in parts):
                    raise NotFound  # pruned meanwhile
                return self.send_parts(
                    parts,
                    manifest.size,
                    manifest.type,
                    f'"{manifest.etag}"',
                    mtime / 1e9,
                    body,
                )
            self.static(names, body)
        except NotFound:
            self.small(
                HTTPStatus.NOT_FOUND, b"Not found\n", "text/plain; charset=utf-8", body
            )

    def manifest(
        self, slug: str, name: str
    ) -> tuple[int, Manifest, list[tuple]] | None:
        """(mtime_ns, manifest, its parts ready to send) for `slug/name`, read once until it
        changes; None if there's no manifest there."""
        file = self.manifests / slug / f"{name}.json"
        try:
            mtime = file.stat().st_mtime_ns
        except (FileNotFoundError, NotADirectoryError):
            return None
        if (cached := self.cache.get(file)) and cached[0] == mtime:
            return cached
        try:
            manifest = Manifest.from_json(file.read_text())
            found = (mtime, manifest, [self.part(p) for p in manifest.parts])
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError, IndexError):
            raise NotFound from None
        if len(self.cache) >= MANIFESTS_CACHED:
            self.cache.pop(next(iter(self.cache)))
        self.cache[file] = found
        return found

    def part(self, p: list) -> tuple:
        """("file", path, offset, length) or ("bytes", data)."""
        if p[0] == "bytes":
            return ("bytes", base64.b64decode(p[1]))
        if not NAME_RE.fullmatch(p[1]):
            raise NotFound
        return ("file", self.audio / p[1], int(p[2]), int(p[3]))

    def static(self, names: list[str], body: bool) -> None:
        target = self.root
        for n in names:
            target = target / n
            try:
                st = os.lstat(target)
            except OSError:
                raise NotFound from None
            if stat.S_ISLNK(st.st_mode):  # never out of the root
                raise NotFound
        if not stat.S_ISREG(st.st_mode):
            raise NotFound
        ctype = (
            TYPES.get(target.suffix.lower())
            or mimetypes.guess_type(target.name)[0]
            or "application/octet-stream"
        )
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        self.send_parts(
            [("file", target, 0, st.st_size)],
            st.st_size,
            ctype,
            etag,
            st.st_mtime,
            body,
        )

    def send_parts(
        self,
        parts: list[tuple],
        size: int,
        ctype: str,
        etag: str,
        mtime: float,
        body: bool,
    ) -> None:
        modified = formatdate(int(mtime), usegmt=True)
        if self.not_modified(etag, int(mtime)):
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.common(etag, modified)
            self.end_headers()
            return
        wanted = None
        if "Range" in self.headers and self.headers.get("If-Range", etag) in (
            etag,
            modified,
        ):
            try:
                wanted = byte_range(self.headers["Range"], size)
            except Unsatisfiable:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.common(etag, modified)
                self.end_headers()
                return
        start, end = wanted or (0, size - 1)
        self.send_response(HTTPStatus.PARTIAL_CONTENT if wanted else HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        if wanted:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.common(etag, modified)
        self.end_headers()
        if body and size:
            try:
                self.write_range(parts, start, end)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                self.close_connection = (
                    True  # the app stopped reading, as it does when seeking
                )

    def not_modified(self, etag: str, mtime: int) -> bool:
        if match := self.headers.get("If-None-Match"):
            return etag in [t.strip() for t in match.split(",")] or match.strip() == "*"
        if since := self.headers.get("If-Modified-Since"):
            try:
                return mtime <= parsedate_to_datetime(since).timestamp()
            except (TypeError, ValueError):
                return False
        return False

    def write_range(self, parts: list[tuple], start: int, end: int) -> None:
        """Send bytes `start` to `end` (inclusive) of the parts put together."""
        self.wfile.flush()
        at = 0
        for p in parts:
            n = p[3] if p[0] == "file" else len(p[1])
            lo, hi = max(start, at), min(end + 1, at + n)
            if lo < hi:
                if p[0] == "bytes":
                    self.wfile.write(p[1][lo - at : hi - at])
                else:
                    with open(p[1], "rb") as f:
                        self.connection.sendfile(f, p[2] + lo - at, hi - lo)
            at += n
            if at > end:
                break

    def common(self, etag: str, modified: str) -> None:
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", modified)
        self.security()

    def security(self) -> None:
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")

    def small(
        self,
        status: HTTPStatus,
        data: bytes,
        ctype: str,
        body: bool,
        headers: dict | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.security()
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        pass  # tailscale serve sees the requests; the sync logs what changes


def main() -> None:
    data = Path("~/.local/share/everythingllm").expanduser()  # hostrpc.data_dir()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8449)
    parser.add_argument("--prefix", default="/podcasts")
    parser.add_argument("--root", type=Path, default=data / "site" / "podcasts")
    parser.add_argument(
        "--manifests", type=Path, default=data / "podcasts" / "manifests"
    )
    parser.add_argument("--audio", type=Path, default=data / "podcasts" / "audio")
    args = parser.parse_args()
    Handler.configure(args.root, args.manifests, args.audio, args.prefix)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(
        f"splice-web on http://{args.host}:{args.port}{args.prefix}/ serving {args.root}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
