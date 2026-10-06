import json
import os
import time

from runs.runlog import MAX_EVENTS, STALE_MS, RunLog, iso, sweep_interrupted


def lines(dir, month="2026-10"):
    return [
        json.loads(line) for line in (dir / f"{month}.jsonl").read_text().splitlines()
    ]


def test_a_run_log_line_has_the_outcome_timing_and_capped_events(tmp_path):
    t = [1791028800.0]  # 2026-10-03T12:00:00Z
    log = RunLog(tmp_path, now=lambda: t[0])
    log.event("Planning")
    t[0] += 4
    for i in range(MAX_EVENTS + 5):
        log.event(f"step {i}")
    t[0] += 6
    file = log.write({"question": "q", "status": "ok", "stats": {"sources": 3}})
    assert file.name == "2026-10.jsonl"
    log.write({"question": "again", "status": "failed"})
    first, second = lines(tmp_path)
    assert first["started"] == "2026-10-03T12:00:00.000Z"
    assert first["seconds"] == 10
    assert first["stats"] == {"sources": 3}
    assert len(first["events"]) == MAX_EVENTS
    assert first["events"][:2] == [[0, "Planning"], [4, "step 0"]]
    assert second["question"] == "again"


def test_a_run_has_a_marker_while_it_runs(tmp_path):
    log = RunLog(tmp_path)
    log.start({"question": "Bitcoin?", "depth": "standard"})
    [marker] = (tmp_path / "running").iterdir()
    note = json.loads(marker.read_text())
    assert (note["question"], note["stale_ms"], "boot" in note) == (
        "Bitcoin?",
        STALE_MS,
        False,
    )
    assert note["started"] == log.started_iso
    log.write({"question": "Bitcoin?", "status": "ok"})
    assert list((tmp_path / "running").iterdir()) == []


def test_the_runner_starting_logs_every_marker_as_interrupted(tmp_path):
    started = time.time() - 120
    RunLog(tmp_path, now=lambda: started).start({"question": "killed"})
    RunLog(tmp_path).start({"question": "also killed"})
    assert sorted(sweep_interrupted(tmp_path, everything=True)) == [
        "also killed",
        "killed",
    ]
    assert list((tmp_path / "running").iterdir()) == []
    month = iso(time.time())[:7]
    swept = {line["question"]: line for line in lines(tmp_path, month)}
    assert {q: line["status"] for q, line in swept.items()} == {
        "killed": "interrupted",
        "also killed": "interrupted",
    }
    assert swept["killed"]["seconds"] >= 119 and swept["killed"]["events"] == []
    assert "stale_ms" not in swept["killed"]


def test_a_new_run_sweeps_only_markers_quiet_for_stale_ms(tmp_path):
    RunLog(tmp_path).start({"question": "quiet"})
    RunLog(tmp_path).start({"question": "fresh"})
    quiet = next(
        f for f in (tmp_path / "running").iterdir() if "quiet" in f.read_text()
    )
    old = time.time() - STALE_MS / 1000 - 5
    os.utime(quiet, (old, old))
    RunLog(tmp_path).start({"question": "next"})
    [line] = lines(tmp_path, iso(time.time())[:7])
    assert (line["question"], line["status"]) == ("quiet", "interrupted")
    assert len(list((tmp_path / "running").iterdir())) == 2
