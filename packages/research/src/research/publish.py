"""Where a finished report goes: a Markdown file in the agent's filesystem folder
(anythingllm-fs) and an entry on the research site."""

from collections.abc import Callable
from pathlib import Path

from sites.store import Entry, unique_slug


def report_file(
    title: str, date: str, question: str, url: str | None, markdown: str
) -> str:
    """The report as one Markdown file: title, what was asked, where it's published, then the report."""
    where = f"Published at {url}" if url else "Not published on the research site."
    return f"# {title}\n\n_{date} · deep research on: {question}_\n\n{where}\n\n{markdown.strip()}\n"


def free_file_slug(dir: Path, title: str) -> str:
    """The title's slug (as the site makes it), with -2, -3, ... when <dir>/<slug>.md is taken."""
    return unique_slug(title, lambda slug: (dir / f"{slug}.md").exists())


def save_report_file(dir: Path, slug: str, text: str) -> Path:
    """Write the report to <dir>/<slug>.md, replacing an older copy with that slug."""
    dir.mkdir(parents=True, exist_ok=True)
    file = dir / f"{slug}.md"
    file.write_text(text, encoding="utf-8")
    return file


def save_then_publish(
    dir: Path,
    title: str,
    text: Callable[[str | None], str],
    publish: Callable[[], Entry],
) -> dict:
    """Save the report file, then publish it. The file comes first so a failed publish (the
    site doesn't keep an entry it couldn't build) doesn't lose the report; once published,
    the file is rewritten with the link. Returns {file?, file_error?, build?, publish_error?}."""
    out: dict = {}
    slug = None
    try:
        slug = free_file_slug(dir, title)
        out["file"] = str(save_report_file(dir, slug, text(None)))
    except OSError as e:
        out["file_error"] = str(e)
    try:
        out["build"] = publish()
    except Exception as e:  # noqa: BLE001 - the report is saved; any publish failure is reported
        out["publish_error"] = str(e)
        return out
    if slug is not None and "file" in out:
        try:
            save_report_file(dir, slug, text(out["build"].url))
        except OSError:
            pass  # the copy without the link stays
    return out
