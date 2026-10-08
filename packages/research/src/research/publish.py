"""Where a finished report goes: a Markdown file in the agent's filesystem folder
(anythingllm-fs/research/<slug>.md). agents-runner adds it to the documents of the workspace
whose chat asked for it (agents.postback), since this container can't reach AnythingLLM."""

import re
import unicodedata
from collections.abc import Callable
from pathlib import Path


def report_file(title: str, date: str, question: str, markdown: str) -> str:
    """The report as one Markdown file: title, what was asked, then the report."""
    return f"# {title}\n\n_{date} · deep research on: {question}_\n\n{markdown.strip()}\n"


def slugify(text: str, max_len: int = 60) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower())[:max_len].strip("-")


def unique_slug(title: str, taken: Callable[[str], bool]) -> str:
    """The title's slug, with -2, -3, ... while `taken(slug)` says it's in use."""
    base = slugify(title) or "report"
    slug, n = base, 2
    while taken(slug):
        slug, n = f"{base}-{n}", n + 1
    return slug


def free_file_slug(dir: Path, title: str) -> str:
    """The title's slug, with -2, -3, ... when <dir>/<slug>.md is taken."""
    return unique_slug(title, lambda slug: (dir / f"{slug}.md").exists())


def save_report(dir: Path, title: str, text: str) -> Path:
    """Write the report to <dir>/<slug>.md, under a slug no other report has."""
    dir.mkdir(parents=True, exist_ok=True)
    file = dir / f"{free_file_slug(dir, title)}.md"
    file.write_text(text, encoding="utf-8")
    return file
