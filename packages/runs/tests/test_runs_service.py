import asyncio

import pytest
from hostrpc import RunnerError
from runs.service import RunService


class Things(RunService):
    ID_PREFIX = "th-"
    NOUN = "thing"
    SUBJECT_KEY = "what"
    MAX_RUNS = 1
    WAIT = 0.2


def test_a_run_reports_progress_and_its_result_to_a_waiting_caller():
    async def main():
        s = Things()
        go = asyncio.Event()

        async def work(run, progress, meter):
            progress("one")
            meter(0.5)
            meter(0.2)  # the bar only moves forward
            await go.wait()
            progress("two")
            return {"status": "ok", "title": "Done thing", "url": "https://x/"}

        run = s.new_run("a thing")
        assert run.id.startswith("th-") and len(run.id) == 11
        assert s.launch(run, work) == 0
        first = await s.op_wait(run.id)
        assert first == {"events": ["one"], "done": False, "result": None}
        assert run.fraction == 0.5
        assert await s.op_wait(run.id, since=1) == {
            "events": [],
            "done": False,
            "result": None,
        }  # timed out
        go.set()
        last = await s.op_wait(run.id, since=1)
        assert last["done"] and last["events"] == ["two"]
        assert (run.title, run.url) == ("Done thing", "https://x/")
        assert await s.op_runs() == {
            "runs": [
                {
                    "run_id": run.id,
                    "what": "a thing",
                    "started": run.started,
                    "done": True,
                }
            ]
        }
        with pytest.raises(RunnerError, match="no thing run 'th-nope' here"):
            await s.op_wait("th-nope")

    asyncio.run(main())


def test_runs_past_max_runs_wait_their_turn_and_a_crash_is_a_failed_result():
    async def main():
        s = Things()
        go = asyncio.Event()

        async def slow(run, progress, meter):
            await go.wait()
            return {"status": "ok"}

        async def crash(run, progress, meter):
            raise ValueError("boom")

        a, b = s.new_run("a"), s.new_run("b")
        assert s.launch(a, slow) == 0
        assert s.launch(b, crash) == 1  # waits for a
        await asyncio.sleep(0.05)
        assert b.events == [
            "Waiting for one of the 1 thing runs going now to finish first."
        ]
        assert not b.done
        go.set()
        result = (await s.op_wait(b.id, since=1))["result"]
        assert result == {"status": "failed", "error": "The thing run failed: boom."}

    asyncio.run(main())


def test_finished_runs_are_dropped_after_result_keep(monkeypatch):
    async def main():
        s = Things()

        async def quick(run, progress, meter):
            return {"status": "ok"}

        run = s.new_run("a")
        s.launch(run, quick)
        await asyncio.sleep(0.01)
        assert run.done and run.id in s.runs
        monkeypatch.setattr(Things, "RESULT_KEEP", -1)
        s.prune()
        assert s.runs == {}

    asyncio.run(main())


def test_a_waiting_caller_counts_as_following(monkeypatch):
    async def main():
        s = Things()
        run = s.new_run("a")
        monkeypatch.setattr(Things, "FOLLOW_GRACE", 0)
        assert not s.followed(run)
        seen = []

        async def work(r, progress, meter):
            await asyncio.sleep(0.05)
            seen.append(s.followed(r))
            return {"status": "ok"}

        s.launch(run, work)
        await s.op_wait(run.id)
        assert seen == [True]

    asyncio.run(main())
