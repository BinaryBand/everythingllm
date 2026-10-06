"""Writing state and served files whole: a reader sees the old file or the new, never half."""

import json
from pathlib import Path

from hostrpc import atomic_write


def _read_json(file: Path):
    """The JSON in `file`; None if there is no such file."""
    try:
        return json.loads(file.read_text())
    except FileNotFoundError:
        return None


def _write_json(file: Path, data) -> None:
    atomic_write(file, json.dumps(data, indent=2) + "\n")
