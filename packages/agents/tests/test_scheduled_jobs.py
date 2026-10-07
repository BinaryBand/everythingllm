import asyncio
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from agents import jobs, runner
from agents.anythingllm import AnythingLLMError, InternalAPI
from hostrpc import RunnerError
from runs import runlog

NOW = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)  # 12:00 in Stockholm
STOCKHOLM = ZoneInfo("Europe/Stockholm")
CHAT = {"workspace": "career", "thread": "default"}


def iso(moment: datetime) -> str:
    return runlog.iso(moment.timestamp())


class FakeJobsAPI:
    """AnythingLLM's internal API, as much of its scheduled jobs as agents-runner uses. It
    takes the token "good"; `login` hands out "old" until asked for a fresh one."""

    def __init__(self):
        self.jobs: dict[int, dict] = {}
        self.runs: dict[int, list[dict]] = {}
        self.calls: list[tuple[str, str]] = []
        self.logins: list[bool] = []
        self.token = "good"
        self.next_id = 1
        self.tools = [
            {
                "category": "agent-skills",
                "items": [
                    {"id": "web-browsing"},
                    {"id": "sql-agent", "requiresSetup": True},
                ],
            },
            {"category": "custom-skills", "items": [{"id": "@@write-entry"}]},
            {"category": "mcp-servers", "items": [{"id": "@@mcp_sites"}]},
        ]

    def add(self, name, schedule="0 18 * * *", tools=None, enabled=True, **more):
        job = {
            "id": self.next_id,
            "name": name,
            "prompt": f"the prompt of {name}",
            "tools": json.dumps(tools) if tools is not None else None,
            "schedule": schedule,
            "enabled": enabled,
            "lastRunAt": None,
            "nextRunAt": None,
            **more,
        }
        self.jobs[job["id"]] = job
        self.runs[job["id"]] = []
        self.next_id += 1
        return job["id"]

    def run(self, job_id, started: datetime, status="completed"):
        self.runs[job_id].insert(
            0,
            {
                "id": len(self.runs[job_id]) + 1,
                "jobId": job_id,
                "status": status,
                "startedAt": iso(started),
                "result": "x" * 10_000,  # a long trace, never to be echoed
            },
        )

    def login(self, fresh: bool) -> dict[str, str]:
        self.logins.append(fresh)
        return {"Authorization": f"Bearer {'good' if fresh else self.token}"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != "Bearer good":
            return httpx.Response(401)
        path = request.url.path.removeprefix("/api/scheduled-jobs")
        self.calls.append((request.method, path))
        body = json.loads(request.content) if request.content else {}
        if path == "" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {**j, "latestRun": (self.runs[i] or [None])[0]}
                        for i, j in self.jobs.items()
                    ]
                },
            )
        if path == "/available-tools":
            return httpx.Response(200, json={"tools": self.tools})
        if path == "/new":
            if not body.get("name", "").strip():
                return httpx.Response(
                    400, json={"job": None, "error": "Name is required"}
                )
            job_id = self.add(body["name"], body["schedule"], body["tools"])
            self.jobs[job_id]["prompt"] = body["prompt"]
            return httpx.Response(201, json={"job": self.jobs[job_id], "error": None})
        parts = path.strip("/").split("/")
        job_id = int(parts[0])
        if parts[1:] == ["runs"]:
            return httpx.Response(200, json={"runs": self.runs.get(job_id, [])})
        if job_id not in self.jobs:
            return httpx.Response(404, json={"job": None, "error": "Job not found"})
        if request.method == "GET":
            return httpx.Response(200, json={"job": self.jobs[job_id]})
        if request.method == "DELETE":
            del self.jobs[job_id]
            self.runs.pop(job_id, None)  # AnythingLLM's cascade
            return httpx.Response(200, json={"success": True})
        if request.method == "PUT":
            self.jobs[job_id].update(body)
            return httpx.Response(200, json={"job": self.jobs[job_id], "error": None})
        return httpx.Response(404)


@pytest.fixture
def api():
    return FakeJobsAPI()


def make(api, tmp_path, now=NOW):
    settings = runner.Settings(runlogs=tmp_path / "runs")
    internal = InternalAPI(
        "http://allm",
        tmp_path / ".env",
        transport=httpx.MockTransport(api),
        login=api.login,
    )
    r = runner.Runner(settings, internal=internal)
    r.scheduled().clock = lambda: now
    return r


def registry(tmp_path) -> list[dict]:
    file = tmp_path / "once.json"
    return json.loads(file.read_text()) if file.exists() else []


def test_a_local_time_becomes_a_utc_cron_across_midnight():
    fire = jobs.fire_time("2026-10-08 00:30", STOCKHOLM, NOW)
    assert fire == datetime(2026, 10, 7, 22, 30, tzinfo=UTC)
    assert jobs.cron(fire) == "30 22 7 10 *"
    winter = jobs.fire_time("2027-01-01T00:15:42", STOCKHOLM, NOW)  # seconds dropped
    assert jobs.cron(winter) == "15 23 31 12 *"


@pytest.mark.parametrize(
    "at, error",
    [
        ("2027-03-28 02:30", "doesn't happen in Europe/Stockholm"),  # spring forward
        ("2026-10-25 02:30", "happens twice in Europe/Stockholm"),  # fall back
        ("2026-10-07 11:59", "has passed"),
        ("2026-10-07 12:00", "has passed"),  # less than a minute away
        ("2027-10-07 12:00", "at most 364 days ahead"),
        ("2026-10-08T09:30+02:00", "without a UTC offset"),
        ("tomorrow at nine", "isn't a date and time"),
    ],
)
def test_times_a_one_off_cant_have(at, error):
    with pytest.raises(RunnerError, match=error):
        jobs.fire_time(at, STOCKHOLM, NOW)


def test_remind_once_shows_first_then_makes_and_registers_the_job(api, tmp_path):
    async def main():
        r = make(api, tmp_path)
        args = {
            "name": "stretch",
            "prompt": "Remind the user to stretch.",
            "at": "2026-10-07 14:05",
            "tools": '["@@mcp_sites"]',
        }
        preview = await r.op_remind_once(CHAT, **args)
        assert (
            '"[once] stretch"' in preview and "Remind the user to stretch." in preview
        )
        assert "Wed 2026-10-07 14:05 CEST (2026-10-07 12:05 UTC" in preview
        assert "@@mcp_sites" in preview and "call again with apply true" in preview
        assert api.jobs == {} and registry(tmp_path) == []
        done = await r.op_remind_once(CHAT, **args, apply=True)
        assert done.startswith('Made one-off job 1 "[once] stretch"')
        job = api.jobs[1]
        assert (job["name"], job["schedule"], json.loads(job["tools"])) == (
            "[once] stretch",
            "5 12 7 10 *",
            ["@@mcp_sites"],
        )
        [entry] = registry(tmp_path)
        assert (entry["id"], entry["fire_at"], entry["state"]) == (
            1,
            "2026-10-07T12:05:00.000Z",
            "pending",
        )
        # A plain reminder has no tools at all, not AnythingLLM's default ones.
        await r.op_remind_once(
            CHAT,
            name="water",
            prompt="Say: drink water.",
            at="2026-10-07 15:00",
            apply=True,
        )
        assert json.loads(api.jobs[2]["tools"]) == []

    asyncio.run(main())


def test_remind_once_refuses_a_name_in_use_a_tool_it_lacks_and_the_wrong_callers(
    api, tmp_path
):
    async def main():
        r = make(api, tmp_path)
        api.add("[once] stretch")
        base = {"prompt": "p", "at": "2026-10-07 14:05"}
        for args, error in [
            ({"name": "[once] stretch"}, 'named "\\[once\\] stretch" already'),
            ({"name": "x", "tools": ["@@nope"]}, "no tool @@nope"),
            ({"name": "x", "tools": "sql-agent"}, "need setting up"),
            ({"name": "", "tools": []}, "short name"),
        ]:
            with pytest.raises(RunnerError, match=error):
                await r.op_remind_once(CHAT, **base, **args, apply=True)
        for scope, error in [
            ({"workspace": "_jobs"}, "a scheduled job can't"),
            ({}, "a scheduled job can't"),
            ({"workspace": "agents-worker"}, "a delegated task can't"),
        ]:
            with pytest.raises(RunnerError, match=error):
                await r.op_remind_once(scope, name="x", **base, apply=True)
            with pytest.raises(RunnerError, match=error):
                await r.op_scheduled_jobs(scope)
        assert list(api.jobs) == [1]

    asyncio.run(main())


def test_list_shows_local_times_one_offs_and_the_repos_job_without_run_results(
    api, tmp_path
):
    async def main():
        r = make(api, tmp_path)
        news = api.add(
            "Daily News Page",
            tools=["@@mcp_sites"],
            nextRunAt="2026-10-07T18:00:00.000Z",
        )
        api.run(news, datetime(2026, 10, 6, 18, 0, 3, tzinfo=UTC))
        await r.op_remind_once(
            CHAT,
            name="call mum",
            prompt="p",
            at="2026-10-07 13:59",
            apply=True,
        )
        text = await r.op_scheduled_jobs(CHAT)
        assert "times in Europe/Stockholm" in text
        assert 'cron "0 18 * * *" (UTC), next Wed 2026-10-07 20:00 CEST' in text
        assert "last run Tue 2026-10-06 20:00 CEST (completed)" in text
        assert "managed by the repo" in text
        assert '"[once] call mum": one-off at Wed 2026-10-07 13:59 CEST' in text
        assert "tools: none" in text and "xxxxxxxx" not in text

    asyncio.run(main())


def test_delete_shows_the_job_first_and_refuses_the_repos_and_a_running_one(
    api, tmp_path
):
    async def main():
        r = make(api, tmp_path)
        news = api.add("Daily News Page")
        mine = api.add("Weekly digest", schedule="0 7 * * 1")
        busy = api.add("Busy")
        api.run(busy, NOW, status="running")
        with pytest.raises(RunnerError, match="managed by the repo"):
            await r.op_scheduled_jobs(CHAT, "delete", news, apply=True)
        with pytest.raises(RunnerError, match="running right now"):
            await r.op_scheduled_jobs(CHAT, "disable", busy, apply=True)
        with pytest.raises(RunnerError, match="no scheduled job 99"):
            await r.op_scheduled_jobs(CHAT, "delete", 99)
        for job_id in (None, True, mine + 0.5, "2.0", "1_2"):
            # int() would delete job 1 for true and job 2 for 2.5
            with pytest.raises(RunnerError, match="give the id"):
                await r.op_scheduled_jobs(CHAT, "delete", job_id, apply=True)
        assert sorted(api.jobs) == [news, mine, busy]
        preview = await r.op_scheduled_jobs(CHAT, "delete", mine)
        assert 'Job 2 "Weekly digest": cron "0 7 * * 1"' in preview
        assert "the prompt of Weekly digest" in preview and "apply true" in preview
        assert mine in api.jobs
        assert (
            await r.op_scheduled_jobs(CHAT, "disable", mine, apply=True)
        ).startswith("Disabled job 2")
        assert api.jobs[mine]["enabled"] is False
        assert "already disabled" in await r.op_scheduled_jobs(CHAT, "disable", mine)
        done = await r.op_scheduled_jobs(CHAT, "delete", str(mine), apply=True)
        assert done == 'Deleted job 2 "Weekly digest".'
        assert sorted(api.jobs) == [news, busy]
        assert ("DELETE", f"/{news}") not in api.calls
        assert ("PUT", f"/{busy}") not in api.calls

    asyncio.run(main())


def test_deleting_a_one_off_by_hand_drops_it_from_the_registry(api, tmp_path):
    async def main():
        r = make(api, tmp_path)
        await r.op_remind_once(
            CHAT, name="x", prompt="p", at="2026-10-07 14:00", apply=True
        )
        assert len(registry(tmp_path)) == 1
        await r.op_scheduled_jobs(CHAT, "delete", 1, apply=True)
        assert registry(tmp_path) == [] and api.jobs == {}

    asyncio.run(main())


def test_the_poller_deletes_a_one_off_only_after_it_has_run(api, tmp_path):
    async def main():
        clock = [NOW]
        r = make(api, tmp_path)
        jobs_ = r.scheduled()
        jobs_.clock = lambda: clock[0]
        await r.op_remind_once(
            CHAT, name="stretch", prompt="p", at="2026-10-07 14:05", apply=True
        )
        fire = datetime(2026, 10, 7, 12, 5, tzinfo=UTC)
        named_only = api.add("[once] not ours", schedule="5 12 7 10 *")
        api.run(named_only, fire)
        # A manual run before fire_at, and fire_at not yet past its grace: nothing to do.
        api.run(1, fire - timedelta(hours=1))
        clock[0] = fire + timedelta(minutes=1)
        assert await jobs_.sweep() == 1 and 1 in api.jobs
        # Past it, with the run still going: wait.
        api.run(1, fire + timedelta(seconds=1), status="running")
        clock[0] = fire + timedelta(minutes=3)
        assert await jobs_.sweep() == 1 and 1 in api.jobs
        api.runs[1][0]["status"] = "completed"
        assert await jobs_.sweep() == 0
        assert 1 not in api.jobs and registry(tmp_path) == []
        assert named_only in api.jobs  # only the registry's jobs are touched
        assert ("DELETE", f"/{named_only}") not in api.calls
        calls = len(api.calls)
        assert await jobs_.sweep() == 0 and len(api.calls) == calls  # empty: no calls

    asyncio.run(main())


def test_a_missed_or_failed_one_off_is_disabled_kept_and_listed(api, tmp_path, caplog):
    async def main():
        r = make(api, tmp_path)
        jobs_ = r.scheduled()
        await r.op_remind_once(
            CHAT, name="missed", prompt="p", at="2026-10-07 14:05", apply=True
        )
        await r.op_remind_once(
            CHAT, name="failed", prompt="p", at="2026-10-07 14:05", apply=True
        )
        fire = datetime(2026, 10, 7, 12, 5, tzinfo=UTC)
        api.run(1, fire - timedelta(hours=1))  # an early manual run doesn't count
        api.run(2, fire, status="failed")
        api.jobs[1]["nextRunAt"] = "2027-10-07T12:05:00.000Z"
        jobs_.clock = lambda: fire + timedelta(minutes=5)
        assert await jobs_.sweep() == 2
        assert [e["state"] for e in registry(tmp_path)] == ["missed", "failed"]
        assert sorted(api.jobs) == [1, 2]
        assert [api.jobs[i]["enabled"] for i in (1, 2)] == [False, False]
        text = await r.op_scheduled_jobs(CHAT)
        assert "MISSED: it never ran, so it was disabled" in text
        assert "its run failed, so it was disabled" in text
        warned = [m for m in caplog.messages if "kept, and listed" in m]
        await jobs_.sweep()  # logged once, not every round
        assert (
            len([m for m in caplog.messages if "kept, and listed" in m])
            == len(warned)
            == 2
        )

    asyncio.run(main())


def test_a_one_off_deleted_elsewhere_leaves_the_registry(api, tmp_path):
    async def main():
        r = make(api, tmp_path)
        await r.op_remind_once(
            CHAT, name="x", prompt="p", at="2026-10-07 14:05", apply=True
        )
        del api.jobs[1]
        r.scheduled().clock = lambda: NOW + timedelta(days=1)
        assert await r.scheduled().sweep() == 0 and registry(tmp_path) == []

    asyncio.run(main())


def test_the_internal_api_logs_in_again_once_after_a_401(api, tmp_path):
    async def main():
        api.token = "old"  # a login that has expired
        r = make(api, tmp_path)
        assert await r.op_scheduled_jobs(CHAT) == "AnythingLLM has no scheduled jobs."
        assert api.logins == [False, True]
        api.login = lambda fresh: {"Authorization": "Bearer wrong"}
        r.internal.login = api.login
        # The service's `errors` hands this text to the caller.
        with pytest.raises(AnythingLLMError, match="refused agents-runner's login"):
            await r.op_scheduled_jobs(CHAT)

    asyncio.run(main())


def test_a_bad_time_zone_says_where_to_fix_it(api, tmp_path):
    async def main():
        r = runner.Runner(
            runner.Settings(runlogs=tmp_path / "runs", timezone="Mars/Olympus"),
            internal=InternalAPI("http://allm", tmp_path / ".env", login=api.login),
        )
        with pytest.raises(RunnerError, match="USER_TIMEZONE is 'Mars/Olympus'"):
            await r.op_scheduled_jobs(CHAT)

    asyncio.run(main())


def test_settings_read_the_time_zone_and_put_the_registry_in_the_data_dir(
    monkeypatch,
):
    monkeypatch.setenv("USER_TIMEZONE", "America/New_York")
    settings = runner.Settings.from_env()
    assert settings.timezone == "America/New_York"
    registry = runner.Runner(settings).scheduled().registry.path
    assert registry.parts[-2:] == ("agents", "once.json")
    monkeypatch.delenv("USER_TIMEZONE")
    assert runner.Settings.from_env().timezone == "Europe/Stockholm"


def test_the_internal_api_finds_anythingllms_env_in_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYTHINGLLM_STORAGE", str(tmp_path))
    client = InternalAPI.from_env()
    assert client.env_file == tmp_path / ".env"
    assert client.api == "http://127.0.0.1:9/api"  # packages/conftest.py's


def test_only_main_starts_the_poller_and_closing_stops_it(api, tmp_path):
    async def main():
        r = make(api, tmp_path)
        assert r.poller is None
        r.start_poller()
        poller = r.poller
        await asyncio.sleep(0)
        assert not poller.done()
        await r.aclose()
        await asyncio.sleep(0)
        assert poller.cancelled()

    asyncio.run(main())


def test_schedule_job_shows_first_then_makes_a_recurring_job(api, tmp_path):
    async def main():
        r = make(api, tmp_path)
        args = {
            "name": "morning news",
            "prompt": "Summarize the headlines.",
            "schedule": "0  6 * * 1-5",
            "tools": ["@@mcp_sites"],
        }
        preview = await r.op_schedule_job(CHAT, **args)
        assert '"morning news"' in preview and 'cron "0 6 * * 1-5"' in preview
        assert "Europe/Stockholm is UTC+2 now" in preview
        assert "call again with apply true" in preview and api.jobs == {}
        done = await r.op_schedule_job(CHAT, **args, apply=True)
        assert done.startswith('Made job 1 "morning news"')
        job = api.jobs[1]
        assert (job["schedule"], json.loads(job["tools"])) == (
            "0 6 * * 1-5",
            ["@@mcp_sites"],
        )
        assert registry(tmp_path) == []  # not a one-off: the poller leaves it be

    asyncio.run(main())


def test_schedule_job_refuses_a_bad_cron_a_one_offs_name_and_the_wrong_callers(
    api, tmp_path
):
    async def main():
        r = make(api, tmp_path)
        api.add("taken")
        base = {"prompt": "p", "schedule": "0 6 * * *"}
        for args, error in [
            ({"name": "x", "schedule": "every morning"}, "cron of five fields"),
            ({"name": "x", "schedule": "0 6 * *"}, "cron of five fields"),
            ({"name": "[once] x"}, "one-off's"),
            ({"name": "taken"}, "already"),
            ({"name": "Daily News Page"}, "the repo manages"),
            ({"name": "x", "tools": ["@@nope"]}, "no tool @@nope"),
        ]:
            with pytest.raises(RunnerError, match=error):
                await r.op_schedule_job(CHAT, **{**base, **args}, apply=True)
        for scope, error in [
            ({"workspace": "_jobs"}, "a scheduled job can't"),
            ({"workspace": "agents-worker"}, "a delegated task can't"),
        ]:
            with pytest.raises(RunnerError, match=error):
                await r.op_schedule_job(scope, name="x", **base, apply=True)
        assert list(api.jobs) == [1]

    asyncio.run(main())


def test_a_job_made_during_a_delegation_but_not_here_is_disabled(api, tmp_path):
    """A delegated task could reach AnythingLLM's own create-scheduled-job, which our
    refusal doesn't cover: whatever appears while a delegation runs is disabled."""

    async def main():
        r = make(api, tmp_path)
        s = r.scheduled()
        before = api.add("mine already")
        notes: list[str] = []
        async with s.guarding("dg-1", notes.append):
            sneaky = api.add("exfiltrate", schedule="* * * * *")
            await r.op_remind_once(
                CHAT, name="ok", prompt="p", at="2026-10-07 14:05", apply=True
            )
        made = max(api.jobs)
        assert api.jobs[before]["enabled"] and api.jobs[made]["enabled"]
        assert not api.jobs[sneaky]["enabled"]
        assert len(notes) == 1 and '"exfiltrate"' in notes[0]
        assert s.watcher is None and s.watched == {}

    asyncio.run(main())


def test_the_watch_disables_a_new_job_while_the_delegation_still_runs(
    api, tmp_path, monkeypatch
):
    monkeypatch.setattr(jobs, "WATCH", 0.01)

    async def main():
        r = make(api, tmp_path)
        s = r.scheduled()
        notes: list[str] = []
        async with s.guarding("dg-1", notes.append):
            sneaky = api.add("exfiltrate")
            for _ in range(100):
                if not api.jobs[sneaky]["enabled"]:
                    break
                await asyncio.sleep(0.01)
            assert not api.jobs[sneaky]["enabled"] and len(notes) == 1
        assert len(notes) == 1  # noted once, not again at the end

    asyncio.run(main())
