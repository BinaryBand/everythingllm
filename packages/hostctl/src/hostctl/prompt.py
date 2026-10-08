"""Which workspaces' EverythingLLM block (hostenv.prompt) is behind the repo's prompt, or
missing.

  check  for health.sh: list the workspaces whose block is behind or missing (notices only)

Standard library only, like the rest of hostctl.
"""

import sys
import urllib.error

from hostenv.prompt import DELEGATED, REPO_PROMPT, version, written_version


def check() -> None:
    """Print a notice for each workspace whose block is behind the repo's, or missing."""
    from hostctl import machine  # logs in to AnythingLLM's internal API

    current = version(REPO_PROMPT.read_text())
    try:
        workspaces = machine.api("GET", "/workspaces")["workspaces"]
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        print(f"  NOTE  couldn't list AnythingLLM's workspaces: {e}")
        return
    notes = []
    for w in workspaces:
        if w["slug"].startswith(DELEGATED):
            continue
        written = written_version(w.get("openAiPrompt"))
        if written is None:
            notes.append(
                f"{w['slug']}: no EverythingLLM block (run update-prompt there)"
            )
        elif written != current:
            notes.append(
                f"{w['slug']}: prompt is version {written}, current {current} (run update-prompt there)"
            )
    for note in notes:
        print(f"  NOTE  {note}")
    if not notes:
        print(f"  OK    workspace prompts current ({current})")


if __name__ == "__main__":
    if sys.argv[1:] != ["check"]:
        sys.exit("usage: python3 -m hostctl.prompt check")
    check()
