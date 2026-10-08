"""Where a finished report goes: a Markdown file in the agent's filesystem folder
(anythingllm-fs/research/<slug>.md). agents-runner adds it to the documents of the workspace
whose chat asked for it (agents.postback), since this container can't reach AnythingLLM."""

import itertools
import re
import unicodedata
from pathlib import Path


def report_file(title: str, date: str, question: str, markdown: str) -> str:
    """The report as one Markdown file: title, what was asked, then the report."""
    return (
        f"# {title}\n\n_{date} · deep research on: {question}_\n\n{markdown.strip()}\n"
    )


def slugify(text: str, max_len: int = 60) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower())[:max_len].strip("-")


def save_report(dir: Path, title: str, text: str) -> Path:
    """Write the report to <dir>/<slug>.md, the title's slug with -2, -3, ... when another
    report has it; one is never replaced."""
    dir.mkdir(parents=True, exist_ok=True)
    base = slugify(title) or "report"
    for n in itertools.count(1):
        file = dir / (f"{base}.md" if n == 1 else f"{base}-{n}.md")
        try:
            with file.open("x", encoding="utf-8") as f:
                f.write(text)
            return file
        except FileExistsError:
            continue
    raise AssertionError("unreachable")
