"""What every app template shares: the error it raises and how it reads text it's given."""

import unicodedata
from typing import Any


class AppError(ValueError):
    """A request or data the template can't take; the message is shown to the agent."""


def text(value: Any, what: str, most: int) -> str:
    """`value` as one line of text, 1 to `most` characters; AppError otherwise. Control
    and invisible characters go, as runs of whitespace do."""
    if not isinstance(value, str):
        raise AppError(f"{what} must be text")
    kept = "".join(
        " " if c.isspace() else c
        for c in value
        if c.isspace() or unicodedata.category(c) not in ("Cc", "Cf", "Co", "Cs", "Cn")
    )
    line = " ".join(kept.split())
    if not line:
        raise AppError(f"{what} is empty")
    if len(line) > most:
        raise AppError(f"{what} is over {most} characters")
    return line
