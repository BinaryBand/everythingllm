"""Heavy runs (Access.heavy: a run asking for longer than containers.NORMAL allows gets HEAVY's
limits, one at a time on the machine), long runs that answer with a run id before they're
done (op_wait has the result), and their run_guard markers."""

import asyncio
import json
import time

import pytest
from sandbox import containers, runner
from sandbox.errors import Busy
from test_runner import (  # noqa: F401 - cfg is a fixture
    A,
    B,
    FakePodman,
    cfg,
    go,
    make,
)

LONG = containers.NORMAL.max_timeout + 1  # a timeout that asks for a heavy run


def heavy(r, scope=A, on=True):
    return go(r.op_access(scope, heavy=on, apply=True, approved=True))


def flag(args, name):
    return args[args.index(name) + 1]


def took(*runs):
    """Seconds the runs took together."""

    async def main():
        t = time.monotonic()
        await asyncio.gather(*runs)
        return time.monotonic() - t

    return go(main())


async def finished(r, scope, run):
    """The run's result, waited for as run-code waits."""
    while run.get("running"):
        run = await r.op_wait(scope, run["run_id"])
    return run


def test_heavy_runs_need_approval(cfg):  # noqa: F811
    r = make(cfg)
    shown = go(r.op_access(A, heavy=True))
    assert shown["would"]["heavy"] is True and shown["needs_approval"] is True
    with pytest.raises(runner.SandboxError, match="needs the user's approval"):
        go(r.op_access(A, heavy=True, apply=True))
    assert heavy(r)["heavy"] is True
    assert json.loads(cfg.access_file.read_text())["career"]["heavy"] is True
    off = go(r.op_access(A, heavy=False, apply=True))  # off: no approval
    assert off["heavy"] is False
    with pytest.raises(runner.SandboxError, match="must be true, false"):
        go(r.op_access(A, heavy="lots"))


def test_a_long_run_of_a_heavy_workspace_gets_more_cpu_memory_and_time(cfg):  # noqa: F811
    r = make(cfg)
    go(r.op_run(A, "bash", "nproc", timeout=9999))
    args, timeout, _ = r.podman.runs()[0]
    assert timeout == containers.NORMAL.max_timeout
    assert (flag(args, "--cpus"), flag(args, "--memory")) == ("1", "1g")
    heavy(r)
    short = go(r.op_run(A, "bash", "ls"))  # a short run stays as it was
    assert short["memory"] == "1g" and flag(r.podman.runs()[1][0], "--cpus") == "1"
    long = go(r.op_run(A, "bash", "nproc", timeout=9999))
    args, timeout, _ = r.podman.runs()[2]
    assert timeout == containers.HEAVY.max_timeout and long["memory"] == "4g"
    assert flag(args, "--cpus") == containers.HEAVY.cpus == "3"
    assert flag(args, "--memory") == flag(args, "--memory-swap") == "4g"
    assert "/tmp:rw,size=1g,mode=1777" in args
    go(r.op_run(B, "bash", "nproc", timeout=9999))  # another workspace: as it was
    assert flag(r.podman.runs()[3][0], "--cpus") == "1"


def test_one_heavy_run_at_a_time_on_the_machine(cfg):  # noqa: F811
    r = runner.Runner(cfg, podman=FakePodman(delay=0.3))
    heavy(r, A)
    heavy(r, B)
    # Two long runs, though in two workspaces, go one after the other.
    assert took(r.op_run(A, "bash", "x", LONG), r.op_run(B, "bash", "x", LONG)) >= 0.6
    # A short run doesn't wait for a heavy one.
    assert took(r.op_run(A, "bash", "x", LONG), r.op_run(B, "bash", "x")) < 0.55


def test_a_long_run_answers_with_its_id_and_holds_the_workspace(cfg, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runner, "WAIT", 0.05)
    r = runner.Runner(cfg, podman=FakePodman(delay=0.4))
    heavy(r)

    async def main():
        started = await r.op_run(A, "bash", "sleep 1\necho done", timeout=3600)
        assert started["running"] and started["timeout"] == 3600
        run_id = started["run_id"]
        marker = cfg.run_markers / f"{run_id}.json"
        note = json.loads(marker.read_text())
        assert note["subject"] == "career: bash: sleep 1" and note["stale_ms"] > 0
        # The workspace is busy: another run, a write, are refused at once, naming it
        # and how long it may take.
        with pytest.raises(Busy, match=rf"{run_id}, at most 3[56]\d\d s more"):
            await r.op_run(A, "bash", "ls")
        with pytest.raises(Busy, match=run_id):
            await r.op_write(A, "/project/x.txt", "x")
        done = await finished(r, A, started)
        assert done["exit_code"] == 0 and done["run_id"] == run_id
        assert not marker.exists()  # gone with the run
        assert (await finished(r, A, await r.op_run(A, "bash", "ls")))["exit_code"] == 0

    go(main())


def test_a_failed_run_keeps_its_error_but_not_its_frames(cfg):  # noqa: F811
    r = make(cfg)

    async def main():
        with pytest.raises(runner.SandboxError) as failed:
            await r.op_run(A, "nolang", "x")  # refused before it starts
        assert failed.value.__traceback__ is not None
        [job] = r._jobs.values() or [None]
        return job

    assert go(main()) is None  # nothing started, nothing kept

    def boom(m):
        raise runner.SandboxError("the run broke")

    r = make(cfg, effect=boom)
    with pytest.raises(runner.SandboxError, match="the run broke"):
        go(r.op_run(A, "bash", "x"))
    [job] = r._jobs.values()
    kept = job.task.exception()
    frames, tb = [], kept.__traceback__
    while tb:
        frames.append(tb.tb_frame.f_code.co_name)
        tb = tb.tb_next
    assert "execute" not in frames and kept.__context__ is None  # its snapshot goes


def test_cleanup_clears_markers_an_earlier_runner_left(cfg):  # noqa: F811
    cfg.run_markers.mkdir(parents=True)
    (cfg.run_markers / "r-old.json").write_text("{}")
    go(make(cfg).cleanup())
    assert not cfg.run_markers.exists()


def test_hostctls_run_guard_sees_a_run_going(cfg, monkeypatch):  # noqa: F811
    from hostctl import run_guard

    monkeypatch.setattr(runner, "WAIT", 0.05)
    r = runner.Runner(cfg, podman=FakePodman(delay=0.3))

    async def main():
        run = await r.op_run(A, "python", "print('hi')")
        [seen] = run_guard.live_runs(cfg.run_markers.parent)
        assert seen["subject"] == "career: python: print('hi')"
        await finished(r, A, run)
        assert run_guard.live_runs(cfg.run_markers.parent) == []

    go(main())
