"""Pictures from the web for the chat, served from the pages site: research-runner's
`images` op, for the image-search skill.

The chat (AnythingLLM's, and the Nilson app's, which loads an image only from the
server's own host) can't show a picture from another site. So the server fetches it here,
in research-runner's container (public hosts only, through the egress proxy), and puts
it on the pages site under FOLDER, where a Markdown image of it shows anywhere the chat
does. The phone never reaches the picture's host; it gets our copy.

A picture is never served as it came: Pillow decodes it (PNG, JPEG, GIF, WebP or AVIF;
an SVG is a page, and isn't taken), and it's saved anew as a JPEG, or a PNG when it's
see-through, at most `max_side` pixels a side, its first frame only. What it carried
besides its pixels (EXIF and GPS data, a comment, a file of another kind glued on) is
left behind. It's named by a hash of the new bytes, so the same picture keeps its address.
Past FOLDER_MAX_BYTES the least recently shown go, and old chats show them broken.

  Pictures.search(query, count)  up to `count` pictures SearXNG's image search finds
                                 (search engines' thumbnails, or the picture when
                                 there's none or it's small), each linking to the page
                                 it's on
  Pictures.show(url, alt)        the one picture at `url`, linking to it

Config (environment):
  PUBLIC_HOST   the pages site's host, in the pictures' addresses (hostenv.pages_url);
                without it there's nowhere to show them
  SEARXNG_URL   the SearXNG to search (publicweb.pages)
  EGRESS_PROXY  the proxy every picture is fetched through (publicweb.public_client)
"""

from __future__ import annotations

import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import chatimage
import httpx
from hostrpc import RunnerError, safefs
from PIL import Image, ImageOps
from publicweb import host_name, public_client, read
from publicweb.pages import USER_AGENT, Search, SearchError

FOLDER = "_webimages"  # on the pages site
FOLDER_MAX_BYTES = 500 << 20
FETCH_MAX_BYTES = 5 << 20  # a picture as it comes
MAX_PIXELS = 40_000_000  # decoded; more is refused before anything is decoded
MIN_SIDE = 32  # smaller is a tracking pixel or an icon
SEARCH_SIDE = 480  # a search's pictures, at most this a side
SMALL = 240  # a thumbnail smaller than this a side: the picture itself is tried
SHOW_SIDE = 1280  # a picture shown by its URL
MAX_COUNT = 6
COUNT = 4
CANDIDATES = 3  # results tried per picture asked for
DEADLINE = 25.0  # seconds for all of an op's fetches; an op answers within 45 s
FETCH_TIMEOUT = 10.0
MAX_TITLE = 100
FORMATS = {"PNG", "JPEG", "GIF", "WEBP", "AVIF"}
# What a browser sends for an <img>. No page's headers, which some image hosts refuse.
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif,image/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Sec-Fetch-Dest": "image",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "cross-site",
}


class ImageError(Exception):
    """A picture that couldn't be fetched or isn't one we take."""


Encoded = tuple[bytes, str, int, int]  # reencode's: the bytes, extension, width, height


def reencode(data: bytes, max_side: int) -> Encoded:
    """The picture in `data` saved anew (see the module's docstring): (bytes, its
    extension, width, height). ImageError unless it's a raster picture we take."""
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.format not in FORMATS:
                raise ImageError(
                    f"not a picture we take ({source.format or 'unknown'})"
                )
            width, height = source.size
            if width * height > MAX_PIXELS:
                raise ImageError(f"too large to decode ({width}×{height})")
            if min(width, height) < MIN_SIDE:
                raise ImageError(f"too small to show ({width}×{height})")
            source.seek(0)  # the first frame of an animation
            if source.format == "JPEG":  # decode at the size it'll be, which is faster
                source.draft("RGB", (max_side, max_side))
            image = ImageOps.exif_transpose(source)  # a copy, decoded
    except ImageError:
        raise
    except Exception as e:  # noqa: BLE001 - whatever Pillow can't read isn't one
        raise ImageError(
            f"not a picture Pillow can read ({type(e).__name__})"
        ) from None
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    see_through = image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    )
    if see_through:
        image = image.convert("RGBA")
        see_through = image.getchannel("A").getextrema()[0] < 255
    out = io.BytesIO()
    if see_through:
        image.save(out, "PNG", optimize=True)
        ext = "png"
    else:
        image.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
        ext = "jpg"
    return out.getvalue(), ext, image.width, image.height


def client() -> httpx.Client:
    """A public_client that asks as a browser asks for an <img>."""
    return public_client(
        ImageError, timeout=httpx.Timeout(FETCH_TIMEOUT), headers=HEADERS
    )


def fetch(http: httpx.Client, url: str, deadline: float) -> bytes:
    """The bytes at `url`, at most FETCH_MAX_BYTES, by `deadline`; ImageError if not."""
    try:
        data, _ = read(http, url, FETCH_MAX_BYTES, ImageError, deadline)
    except ImageError as e:
        raise ImageError(f"{host_name(url)}: {e}") from None
    except httpx.HTTPStatusError as e:
        raise ImageError(
            f"{host_name(url)} answered {e.response.status_code}"
        ) from None
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise ImageError(
            f"couldn't reach {host_name(url)} ({e or type(e).__name__})"
        ) from None
    return data


def title(text: str, fallback: str) -> str:
    """One line of at most MAX_TITLE characters, for the picture's alt text."""
    text = chatimage.clean(text) or chatimage.clean(fallback) or "picture"
    return text if len(text) <= MAX_TITLE else text[: MAX_TITLE - 1].rstrip() + "…"


class Pictures:
    """The ops' side of a runner: where pictures go, the search, and one client and one
    pool to fetch them with for the runner's life. `search` is make_image_search's."""

    def __init__(
        self,
        folder: Path,
        pages_url: str,
        search: Search,
        http: httpx.Client | None = None,
    ):
        self.folder = folder
        self.pages_url = pages_url
        self.find = search
        self.http = http or client()
        self.pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="images")

    def base(self) -> str:
        """The pictures' address; RunnerError if there's no pages site."""
        if not self.pages_url:
            raise RunnerError(
                "the pages site has no address (PUBLIC_HOST isn't set), so there's "
                "nowhere to show pictures"
            )
        return f"{self.pages_url.rstrip('/')}/{FOLDER}/"

    def save(self, base: str, taken: list[tuple[Encoded, str, str]]) -> list[dict]:
        """Save each (picture, alt, link), trim the folder once, and describe them for
        the skill."""
        self.folder.mkdir(mode=0o755, parents=True, exist_ok=True)
        out = []
        with safefs.folder(self.folder) as d:
            for (data, ext, width, height), alt, link in taken:
                url = base + quote(safefs.keep(d, data, ext))
                out.append(
                    {
                        "image": chatimage.linked_image(alt, url, link),
                        "url": url,
                        "page": link,
                        "source": host_name(link),
                        "width": width,
                        "height": height,
                    }
                )
            safefs.trim(d, FOLDER_MAX_BYTES)
        return out

    def search(self, query: str, count: int = COUNT) -> dict:
        """Up to `count` pictures for `query`: {images: [...], skipped: [why, ...]}, the
        images in the search's order."""
        base = self.base()
        deadline = time.monotonic() + DEADLINE
        try:
            results = self.find(query)
        except SearchError as e:
            raise RunnerError(f"the image search failed: {e}") from None
        if not results:
            raise RunnerError(f'the image search found nothing for "{query}"')
        enough = threading.Event()

        def one(result: dict) -> Encoded:
            """The result's thumbnail, or its picture when that fails or is small."""
            reasons, best = [], None
            for source in dict.fromkeys(
                u for u in (result["thumb"], result["full"]) if u
            ):
                if enough.is_set():
                    break
                try:
                    picture = reencode(fetch(self.http, source, deadline), SEARCH_SIDE)
                except ImageError as e:
                    reasons.append(str(e))
                    continue
                if best is None or max(picture[2:]) > max(best[2:]):
                    best = picture
                if max(best[2:]) >= SMALL:
                    break
            if best is None:
                raise ImageError("; ".join(reasons) or "no picture")
            return best

        # Fetched at once and taken in the search's order; once there are enough, what's
        # still waiting is dropped and what's fetching stops at its next step.
        candidates = results[: count * CANDIDATES]
        futures = [self.pool.submit(one, r) for r in candidates]
        taken, skipped, seen = [], [], set()  # seen: the pictures' bytes, taken once
        try:
            for result, future in zip(candidates, futures, strict=True):
                if len(taken) >= count:
                    break
                try:
                    picture = future.result()
                except ImageError as e:
                    skipped.append(f"{host_name(result['page'])}: {e}")
                    continue
                if picture[0] not in seen:
                    seen.add(picture[0])
                    alt = title(result["title"], query)
                    taken.append((picture, alt, result["page"]))
        finally:
            enough.set()
            for future in futures:
                future.cancel()
        if not taken:
            raise RunnerError(
                f'none of the pictures found for "{query}" could be fetched: '
                + "; ".join(skipped[:3])
            )
        return {"images": self.save(base, taken), "skipped": skipped}

    def show(self, url: str, alt: str = "") -> dict:
        """The picture at `url`: {images: [one], skipped: []}."""
        base = self.base()
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise RunnerError("url must be an http or https address of a picture")
        try:
            data = fetch(self.http, url, time.monotonic() + DEADLINE)
            picture = reencode(data, SHOW_SIDE)
        except ImageError as e:
            raise RunnerError(f"couldn't show {url}: {e}") from None
        taken = [(picture, title(alt, host_name(url)), url)]
        return {"images": self.save(base, taken), "skipped": []}
