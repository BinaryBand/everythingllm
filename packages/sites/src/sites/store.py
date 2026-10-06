"""Content store for the Zola sites: one `<site>/<section>/<slug>.md` per entry.

A site's source (zola.toml, templates, section `_index.md` files) lives in the
repo; only entries live here. A section's `[extra]` can carry `agent_readonly = true`
(the agent may read the section but not change it) and `[extra.audit]` (checks for the
audit server). Entries are Markdown with JSON front matter
(JSON is valid YAML, which Zola reads between `---` lines), so the agent fills
in fields and the site's templates do all the HTML. Writing or deleting an
entry rebuilds that site (see sites.build), so it's live when the call returns;
when the site doesn't build, the change is undone.
"""

import json
import os
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import tomllib
from hostrpc import atomic_write, env_values

MAX_BYTES = 200 * 1024
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TERA_RE = re.compile(r"\{(?=[{%#])")  # the start of `{{`, `{%` or `{#`
# What YAML won't take raw in a string: DEL and the C1 controls, the line and paragraph
# separators, the byte order mark, the non-characters U+FFFE and U+FFFF, and surrogates.
YAML_UNSAFE = re.compile("[\x7f-\x9f\u2028\u2029\ufeff\ufffe\uffff\ud800-\udfff]")


# The user's time zone, which entries and reports are dated by: a scheduled job's clock is
# UTC, and at 23:00 UTC it's already the next day in Stockholm.
STOCKHOLM = ZoneInfo("Europe/Stockholm")


def today(now: datetime | None = None) -> str:
    """Today's date in the user's time zone."""
    return (
        (now.astimezone(STOCKHOLM) if now else datetime.now(STOCKHOLM))
        .date()
        .isoformat()
    )


PAGES_PORT = 8445  # the pages site, which serves every built site under /<name>/


def host_file(source: Path) -> Path:
    """host.env at the root of the repo that holds the sites (<repo>/packages/sites/zola/sites). The
    container sees it at /mcp/host.env, which matters for builders that get none of our
    environment, such as sites-write run from a script."""
    return source.parents[3] / "host.env"


def pages_url(source: Path) -> str:
    """The pages site's public URL, from PUBLIC_HOST in the environment or else in
    host.env; "" when neither has it."""
    host = env_values(host_file(source), ["PUBLIC_HOST"]).get("PUBLIC_HOST")
    return f"https://{host}:{PAGES_PORT}/" if host else ""


def site_url(source: Path, name: str) -> str:
    """A site's public URL (see pages_url); "" when there's no PUBLIC_HOST, and the
    site's zola.toml base_url applies."""
    base = pages_url(source)
    return f"{base}{name}/" if base else ""


def slugify(text: str, max_len: int = 60) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower())[:max_len].strip("-")


def unique_slug(title: str, taken: Callable[[str], bool]) -> str:
    """The title's slug, with -2, -3, ... while `taken(slug)` says it's in use."""
    base = slugify(title) or "entry"
    slug, n = base, 2
    while taken(slug):
        slug, n = f"{base}-{n}", n + 1
    return slug


class SiteError(ValueError):
    """A request the caller can fix; the message is shown to the model."""


@dataclass
class Site:
    name: str
    title: str
    description: str
    url: str
    sections: list[str]
    help: str
    section_extra: dict[str, dict] = field(
        default_factory=dict
    )  # each section's [extra]

    @property
    def readonly(self) -> list[str]:
        return [
            s
            for s in self.sections
            if self.section_extra.get(s, {}).get("agent_readonly")
        ]


@dataclass
class Entry:
    section: str
    slug: str
    title: str
    date: str
    url: str


def _name(kind: str, value: object) -> str:
    if not isinstance(value, str) or not NAME_RE.fullmatch(value):
        raise SiteError(
            f"{kind} must be 1-63 lowercase letters, digits or hyphens, "
            "not starting or ending with a hyphen."
        )
    return value


FRONT_MATTER = re.compile(r"\+\+\+\n(.*?)^\+\+\+$", re.DOTALL | re.MULTILINE)


def _section_extra(index: Path) -> dict:
    """The `[extra]` table of a section's `_index.md` (TOML between `+++` lines)."""
    m = FRONT_MATTER.match(index.read_text(encoding="utf-8"))
    return tomllib.loads(m.group(1)).get("extra", {}) if m else {}


def _split(text: str) -> tuple[dict, str]:
    """Front matter and body of an entry file written by this store."""
    head, sep, body = text.removeprefix("---\n").partition("\n---\n")
    if not sep:
        return {}, text
    try:
        return json.loads(head), body
    except json.JSONDecodeError:
        return {}, body


class SiteStore:
    def __init__(
        self,
        source: Path | str,
        content: Path | str,
        build: Callable[[str], object] | None = None,
        agent: bool = False,
    ):
        """`build(site_name)` publishes a site after a change; it raises on failure.
        `agent`: the caller is the agent, so read-only sections refuse changes."""
        self.source = Path(source).resolve(strict=True)
        self.content = Path(content).resolve(strict=True)
        self.build = build
        self.agent = agent

    def config(self, name: str) -> dict:
        """A site's zola.toml."""
        return tomllib.loads(
            (self.source / _name("site", name) / "zola.toml").read_text()
        )

    def site(self, name: str) -> Site:
        _name("site", name)
        path = self.source / name / "zola.toml"
        if not path.is_file():
            known = ", ".join(s.name for s in self.sites()) or "none"
            raise SiteError(f"no site named '{name}' (sites: {known}).")
        config = self.config(name)
        sections = sorted(
            p.parent.name
            for p in (self.source / name / "content").glob("*/_index.md")
            if NAME_RE.fullmatch(p.parent.name)
        )
        section_extra = {
            s: _section_extra(self.source / name / "content" / s / "_index.md")
            for s in sections
        }
        extra = config.get("extra", {})
        return Site(
            name,
            config.get("title", name),
            config.get("description", ""),
            site_url(self.source, name) or config["base_url"].rstrip("/") + "/",
            list(section_extra),
            extra.get("agent_help", "").strip(),
            section_extra,
        )

    def _section(self, site: Site, section: str) -> Path:
        _name("section", section)
        if section not in site.sections:
            raise SiteError(
                f"site '{site.name}' has no section '{section}' "
                f"(sections: {', '.join(site.sections) or 'none'})."
            )
        return self.content / site.name / section

    def _changeable(self, site: Site, section: str) -> Path:
        folder = self._section(site, section)
        if self.agent and section in site.readonly:
            raise SiteError(
                f"section '{section}' is written by {site.name}'s own tooling; "
                "agents can read it but not change it."
            )
        return folder

    def sites(self) -> list[Site]:
        return [
            self.site(p.name)
            for p in sorted(self.source.iterdir())
            if (p / "zola.toml").is_file() and NAME_RE.fullmatch(p.name)
        ]

    def entries(self, site_name: str, section: str = "") -> list[Entry]:
        site = self.site(site_name)
        sections = [section] if section else site.sections
        out = []
        for name in sections:
            for file in self._section(site, name).glob("*.md"):
                meta, _ = _split(file.read_text(encoding="utf-8"))
                out.append(
                    Entry(
                        name,
                        file.stem,
                        meta.get("title", file.stem),
                        str(meta.get("date", "")),
                        f"{site.url}{name}/{file.stem}/",
                    )
                )
        return sorted(out, key=lambda e: (e.date, e.slug), reverse=True)

    def get(self, site_name: str, section: str, slug: str) -> tuple[Entry, dict, str]:
        site = self.site(site_name)
        file = self._section(site, section) / f"{_name('slug', slug)}.md"
        if not file.is_file():
            raise SiteError(f"no entry '{slug}' in {site_name}/{section}.")
        meta, body = _split(file.read_text(encoding="utf-8"))
        entry = Entry(
            section,
            slug,
            meta.get("title", slug),
            str(meta.get("date", "")),
            f"{site.url}{section}/{slug}/",
        )
        return entry, meta.get("extra", {}), body

    def free_slug(self, site_name: str, section: str, title: str) -> str:
        """A slug from the title that no entry in the section has yet."""

        def taken(slug: str) -> bool:
            try:
                self.get(site_name, section, slug)
            except SiteError:
                return False
            return True

        return unique_slug(title, taken)

    def write(
        self,
        site_name: str,
        section: str,
        slug: str | None,
        title: str,
        date: str,
        extra: dict | None = None,
        body: str = "",
        overwrite: bool = False,
    ) -> Entry:
        """Save an entry and build its site (nothing is kept when it doesn't build); with
        no slug, under a free one made from the title."""
        site = self.site(site_name)
        folder = self._changeable(site, section)
        if not isinstance(title, str) or not title.strip():
            raise SiteError("title must be a non-empty string.")
        slug = slug or self.free_slug(site_name, section, title)
        file = folder / f"{_name('slug', slug)}.md"
        try:
            # fromisoformat alone also takes '20261003' and '2026-W40-6'.
            if not DATE_RE.fullmatch(date):
                raise ValueError
            Date.fromisoformat(date)
        except (TypeError, ValueError):
            raise SiteError("date must be YYYY-MM-DD, e.g. '2026-10-03'.") from None
        if extra is not None and not isinstance(extra, dict):
            raise SiteError("extra must be an object of fields.")
        if file.exists() and not overwrite:
            raise SiteError(
                f"entry '{slug}' already exists in {site_name}/{section}. Call again "
                "with overwrite=true to replace it, or pick a different slug."
            )
        # `slug` stops Zola taking a leading date off the file name
        # (2026-10-01-notes.md would be served at /notes/), so the URL is the one returned.
        meta = {
            "title": " ".join(title.split()),
            "date": date,
            "slug": slug,
            "extra": extra or {},
        }
        # Zola passes raw HTML in Markdown through untouched; the site's
        # templates are the only HTML.
        body = (body or "").replace("<", "&lt;")
        # Zola also runs `{{ }}`, `{% %}` and `{# #}` in the body as Tera/shortcodes, even
        # inside backticks: `{{ get_env(name=...) }}` printed the server's secrets and
        # load_data could read files and URLs. A zero-width space between the two
        # characters renders the same and no longer matches.
        body = TERA_RE.sub("{\u200b", body)
        text = f"---\n{front_matter(meta)}\n---\n{body.strip()}\n"
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise SiteError(f"entry is larger than {MAX_BYTES // 1024} KB.")
        folder.mkdir(parents=True, exist_ok=True)
        old = (file.read_text(encoding="utf-8"), file.stat()) if file.exists() else None
        atomic_write(file, text)
        try:
            self.publish(site.name)
        except SiteError:
            if old is None:
                file.unlink(missing_ok=True)
            else:
                _restore(file, *old)
            raise
        return Entry(section, slug, meta["title"], date, f"{site.url}{section}/{slug}/")

    def delete(self, site_name: str, section: str, slug: str) -> None:
        site = self.site(site_name)
        file = self._changeable(site, section) / f"{_name('slug', slug)}.md"
        if not file.is_file():
            raise SiteError(f"no entry '{slug}' in {site_name}/{section}.")
        old = file.read_text(encoding="utf-8"), file.stat()
        file.unlink()
        try:
            self.publish(site.name)
        except SiteError:
            _restore(file, *old)
            raise

    def publish(self, site_name: str) -> None:
        """Rebuild a site after a change. When this raises, the caller undoes the change, so
        what's stored is always a site that builds. No `build` publishes nothing (tests)."""
        if self.build is None:
            return
        try:
            self.build(site_name)
        except Exception as e:  # noqa: BLE001 - BuildError, or anything else the builder hit
            raise SiteError(f"not saved: the site didn't build: {e}") from None


def _restore(file: Path, text: str, st: os.stat_result) -> None:
    """Put back an entry a failed build undid, with its old mtime, so the audit's
    entries-newer-than-the-build check doesn't flag the site."""
    atomic_write(file, text)
    os.utime(file, ns=(st.st_atime_ns, st.st_mtime_ns))


def front_matter(meta: dict) -> str:
    """`meta` as JSON that Zola's YAML parser reads too. YAML has no surrogate-pair escapes,
    so ASCII-only JSON's `\\ud83d\\udc4b` for an emoji fails the build: characters go in
    as they are, and only those YAML doesn't allow raw get a `\\u` escape, which both read.
    JSON already escapes the C0 controls; a lone surrogate (no character) becomes U+FFFD."""
    return YAML_UNSAFE.sub(
        lambda m: "\\ufffd" if "\ud800" <= m[0] <= "\udfff" else f"\\u{ord(m[0]):04x}",
        json.dumps(meta, ensure_ascii=False, indent=1),
    )
