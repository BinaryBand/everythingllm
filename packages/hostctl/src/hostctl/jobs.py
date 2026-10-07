"""The scheduled jobs the repo manages: anythingllm/scheduled-jobs/<slug>/, each a job.json
(name, schedule, tools) and a prompt.md. Deploy (hostctl.sync) writes them into AnythingLLM,
matched by name, and agents-runner's scheduled-jobs op won't delete or disable them.

Standard library only, like the rest of hostctl; agents-runner imports it too, so importing
it reads nothing.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
REPO_JOBS = ROOT / "anythingllm" / "scheduled-jobs"


def repo_jobs(folder: Path = REPO_JOBS) -> dict[str, dict]:
    """Jobs in the repo by name: <slug>/job.json (name, schedule, tools) + prompt.md."""
    jobs = {}
    if folder.is_dir():
        for d in sorted(p for p in folder.iterdir() if p.is_dir()):
            job = json.loads((d / "job.json").read_text())
            job.setdefault("tools", [])
            job["prompt"] = (d / "prompt.md").read_text().strip()
            jobs[job["name"]] = job
    return jobs


def duplicates(names: list[str]) -> list[str]:
    """The names that occur more than once, in order."""
    seen: set[str] = set()
    twice = []
    for name in names:
        if name in seen and name not in twice:
            twice.append(name)
        seen.add(name)
    return twice
