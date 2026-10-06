"""The handoff's acceptance list, against a scripted AnythingLLM: `Upstream` stands in for
relay.upstream.answer (its events, when it's closed and how often it's called), and
test_upstream.py covers the real one's parsing of stream-chat. Each test runs the app's
own lifespan, as uvicorn would."""

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from relay.app import Config, create_app
from relay.runs import RESTARTED
from relay.store import Store

from relay import upstream

KEY = "allm-key-0123456789"
TOKEN = "relay-token-abcdef"
BODY = {
    "workspace": "planning",
    "thread": "t1",
    "message": "What's next?",
    "mode": "query",
    "clientId": "c_1",
}
DONE = ("done", {"citations": []})


def go(coro):
    return asyncio.run(asyncio.wait_for(coro, 10))


class Upstream:
    """A scripted answer: yields `script` (events, or an asyncio.Event to wait on) and
    records how often it was called and whether the relay closed it early."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0
        self.closed = False
        self.finished = False

    async def __call__(self, workspace, thread, question, mode):
        self.calls += 1
        try:
            for step in self.script:
                if isinstance(step, asyncio.Event):
                    await step.wait()
                    continue
                if step[0] in ("done", "failed"):
                    self.finished = True  # the relay hangs up once it has the end
                yield step
            self.finished = True
        finally:
            if not self.finished:
                self.closed = True


class Notified(list):
    """A notify_finished that records each finished run's status and question."""

    async def __call__(self, run, question):
        self.append((run["status"], question))


def text(t):
    return ("text", {"text": t})


def parse(stream: str) -> list[tuple[int, str, dict]]:
    """Server-sent events back into (id, event, data), skipping comments."""
    out = []
    for block in stream.strip().split("\n\n"):
        lines = dict(
            line.split(": ", 1)
            for line in block.splitlines()
            if not line.startswith(":")
        )
        if lines:
            out.append((int(lines["id"]), lines["event"], json.loads(lines["data"])))
    return out


@contextlib.asynccontextmanager
async def running(tmp_path, answer, notify_finished=None):
    """The app started as uvicorn would, and a client that sends the relay's token."""
    app = create_app(
        Config(api_key=KEY, token=TOKEN, database=tmp_path / "relay.db"),
        answer,
        notify_finished,
    )
    relay = app.state.relay
    relay.ping = 0.05
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://relay",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client,
    ):
        yield relay, client


async def finished(relay, run_id):
    while relay.store.get(run_id)["status"] == "running":
        await asyncio.sleep(0.01)


async def post_run(client, **changes):
    return await client.post("/runs", json={**BODY, **changes})


# 1
def test_a_run_streams_its_pieces_then_done_with_the_citations(tmp_path):
    answer = Upstream(text("Hel"), text("lo"), ("done", {"citations": ["Doc A"]}))
    notified = Notified()

    async def main():
        async with running(tmp_path, answer, notified) as (relay, client):
            r = await post_run(client)
            assert r.status_code == 201
            run = r.json()
            assert run["id"].startswith("r_") and run["clientId"] == "c_1"
            assert (run["status"], run["finishedAt"]) == ("running", None)
            await finished(relay, run["id"])
            r = await client.get(f"/runs/{run['id']}/events")
            assert r.headers["content-type"].startswith("text/event-stream")
            assert parse(r.text) == [
                (1, "text", {"text": "Hel"}),
                (2, "text", {"text": "lo"}),
                (3, "done", {"citations": ["Doc A"]}),
            ]
            got = (await client.get(f"/runs/{run['id']}")).json()
            assert got["status"] == "done" and got["finishedAt"]

    go(main())
    assert notified == [("done", "What's next?")]


# 2
def test_a_follower_leaving_doesnt_stop_the_answer(tmp_path):
    async def main():
        gate = asyncio.Event()
        answer = Upstream(text("one"), gate, text("two"), DONE)
        async with running(tmp_path, answer) as (relay, client):
            run = (await post_run(client)).json()
            follower = relay.follow(run["id"])
            assert parse(await anext(follower)) == [(1, "text", {"text": "one"})]
            await follower.aclose()  # the app goes away mid-answer
            await asyncio.sleep(0.1)
            assert not answer.closed
            assert relay.store.get(run["id"])["status"] == "running"
            gate.set()
            await finished(relay, run["id"])
            assert answer.finished and not answer.closed
            events = relay.store.events(run["id"])
            assert [e[1] for e in events] == ["text", "text", "done"]

    go(main())


# 3
def test_rejoining_with_last_event_id_gets_exactly_what_came_after(tmp_path):
    async def main():
        gate = asyncio.Event()
        answer = Upstream(text("a"), text("b"), gate, text("c"), DONE)
        async with running(tmp_path, answer) as (relay, client):
            run = (await post_run(client)).json()
            while len(relay.store.events(run["id"])) < 2:
                await asyncio.sleep(0.01)
            # Live: rejoin after event 1, while the answer is still going.
            follower = relay.follow(run["id"], after=1)
            assert parse(await anext(follower)) == [(2, "text", {"text": "b"})]
            gate.set()
            rest = [e async for e in follower if not e.startswith(":")]
            assert [e[:2] for e in parse("".join(rest))] == [(3, "text"), (4, "done")]
            # Finished: through the API.
            r = await client.get(
                f"/runs/{run['id']}/events", headers={"Last-Event-ID": "2"}
            )
            assert [e[:2] for e in parse(r.text)] == [(3, "text"), (4, "done")]
            r = await client.get(f"/runs/{run['id']}/events")
            assert [e[0] for e in parse(r.text)] == [1, 2, 3, 4]

    go(main())


# 4
def test_the_same_client_id_returns_the_same_run_and_calls_upstream_once(tmp_path):
    answer = Upstream(text("x"), DONE)

    async def main():
        async with running(tmp_path, answer) as (relay, client):
            first, again = await post_run(client), await post_run(client)
            assert (first.status_code, again.status_code) == (201, 200)
            assert first.json()["id"] == again.json()["id"]
            await finished(relay, first.json()["id"])
            # Even after it ended, and even on another thread.
            later = await post_run(client, thread="t2")
            assert later.status_code == 200 and later.json()["id"] == first.json()["id"]

    go(main())
    assert answer.calls == 1


# 5
def test_a_thread_takes_one_running_run_at_a_time(tmp_path):
    async def main():
        gate = asyncio.Event()
        async with running(tmp_path, Upstream(gate, DONE)) as (relay, client):
            first = await post_run(client)
            second = await post_run(client, clientId="c_2")
            assert second.status_code == 409 and "error" in second.json()
            other = await post_run(client, clientId="c_3", thread="t2")
            assert other.status_code == 201
            gate.set()
            await finished(relay, first.json()["id"])
            assert (await post_run(client, clientId="c_4")).status_code == 201

    go(main())


# 6
def test_cancel_closes_the_connection_and_ends_with_cancelled(tmp_path):
    async def main():
        gate = asyncio.Event()
        answer = Upstream(text("so far"), gate, text("never"))
        async with running(tmp_path, answer) as (relay, client):
            run = (await post_run(client)).json()
            follower = relay.follow(run["id"])
            await anext(follower)
            r = await client.post(f"/runs/{run['id']}/cancel")
            assert r.status_code == 200 and r.json()["status"] == "cancelled"
            assert answer.closed
            rest = [e async for e in follower if not e.startswith(":")]
            assert parse("".join(rest)) == [(2, "cancelled", {})]
            # On a run that has ended, cancel changes nothing.
            again = await client.post(f"/runs/{run['id']}/cancel")
            assert again.status_code == 200 and again.json() == r.json()
            assert len(relay.store.events(run["id"])) == 2
            assert (await client.post("/runs/r_nope/cancel")).status_code == 404

    go(main())


# 7, through the relay; test_upstream.py has each case from the wire
@pytest.mark.parametrize(
    "event",
    [
        ("failed", {"error": "Model overloaded"}),
        ("failed", {"error": upstream.ABORTED}),
        ("failed", {"error": upstream.status_error(500)}),
    ],
)
def test_an_upstream_failure_fails_the_run(tmp_path, event):
    notified = Notified()

    async def main():
        async with running(tmp_path, Upstream(text("pa"), event), notified) as (
            relay,
            client,
        ):
            run = (await post_run(client)).json()
            await finished(relay, run["id"])
            r = await client.get(f"/runs/{run['id']}/events")
            assert parse(r.text)[-1] == (2, "failed", event[1])
            assert relay.store.get(run["id"])["status"] == "failed"

    go(main())
    assert notified == [("failed", "What's next?")]


def test_an_answer_that_just_stops_is_done(tmp_path):
    async def main():
        async with running(tmp_path, Upstream(text("all"))) as (relay, client):
            run = (await post_run(client)).json()
            await finished(relay, run["id"])
            assert relay.store.events(run["id"])[-1] == (2, *DONE)

    go(main())


# 8
def test_a_restart_mid_run_fails_it_and_keeps_its_events(tmp_path):
    async def first_life():
        gate = asyncio.Event()
        async with running(tmp_path, Upstream(text("partial"), gate)) as (
            relay,
            client,
        ):
            run = (await post_run(client)).json()
            while not relay.store.events(run["id"]):
                await asyncio.sleep(0.01)
        return run["id"]  # the relay stopped mid-answer

    run_id = go(first_life())

    async def second_life():
        async with running(tmp_path, Upstream()) as (_, client):
            run = (await client.get(f"/runs/{run_id}")).json()
            assert run["status"] == "failed" and run["finishedAt"]
            r = await client.get(f"/runs/{run_id}/events")
            assert parse(r.text) == [
                (1, "text", {"text": "partial"}),
                (2, "failed", {"error": RESTARTED}),
            ]
            assert (await client.get("/runs?status=running")).json() == []

    go(second_life())


# 9
def test_no_body_or_log_line_carries_the_key_or_the_token(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    transport = httpx.MockTransport(lambda request: httpx.Response(401))
    allm = httpx.AsyncClient(transport=transport)

    def answer_401(workspace, thread, question, mode):
        return upstream.answer(
            allm, "http://allm", KEY, workspace, thread, question, mode
        )

    bodies = []

    async def main():
        async with running(tmp_path, answer_401) as (relay, client):
            run = await post_run(client)
            bodies.append(run.text)
            no_token = {"Authorization": ""}
            for r in [
                await client.post("/runs", json=BODY, headers=no_token),
                await client.post("/runs", json={"nope": 1}),
                await client.get(
                    "/runs/r_x", headers={"Authorization": "Bearer wrong"}
                ),
            ]:
                bodies.append(r.text)
            run_id = run.json()["id"]
            await finished(relay, run_id)
            for path in (f"/runs/{run_id}", f"/runs/{run_id}/events", "/runs"):
                bodies.append((await client.get(path)).text)
        await allm.aclose()

    go(main())
    assert upstream.status_error(401) in bodies[5]  # it did fail, with the message
    for text_ in [*bodies, caplog.text]:
        assert KEY not in text_ and TOKEN not in text_


# --- the rest of the API ---


def test_every_route_but_health_needs_the_token(tmp_path):
    async def main():
        async with running(tmp_path, Upstream()) as (_, client):
            del client.headers["Authorization"]
            assert (await client.get("/health")).json() == {"ok": True}
            for method, path in [
                ("GET", "/runs"),
                ("POST", "/runs"),
                ("GET", "/runs/r_x"),
                ("GET", "/runs/r_x/events"),
                ("POST", "/runs/r_x/cancel"),
            ]:
                for headers in (
                    {},
                    {"Authorization": "Bearer nope"},
                    {"Authorization": TOKEN},
                ):
                    r = await client.request(method, path, headers=headers)
                    assert r.status_code == 401, (method, path, headers)
                    assert r.json() == {"error": "A valid relay token is required."}

    go(main())


def test_listing_unknown_runs_and_bad_bodies(tmp_path):
    async def main():
        gate = asyncio.Event()
        async with running(tmp_path, Upstream(gate, DONE)) as (relay, client):
            a = (await post_run(client)).json()
            b = (await post_run(client, clientId="c_2", thread="t2")).json()
            running_ = (await client.get("/runs?status=running")).json()
            assert [r["id"] for r in running_] == [a["id"], b["id"]]
            assert (await client.get("/runs?status=odd")).status_code == 400
            gate.set()
            await finished(relay, a["id"])
            await finished(relay, b["id"])
            assert (await client.get("/runs?status=running")).json() == []
            assert len((await client.get("/runs")).json()) == 2
            for path in ("/runs/r_nope", "/runs/r_nope/events"):
                r = await client.get(path)
                assert r.status_code == 404 and r.json() == {"error": "No such run."}
            for body in ({**BODY, "message": " "}, {**BODY, "mode": "agent"}, ["x"]):
                r = await client.post("/runs", json=body)
                assert r.status_code == 400 and "error" in r.json()
            assert (await client.post("/runs", content=b"{")).status_code == 400

    go(main())


def test_a_quiet_run_is_pinged(tmp_path):
    async def main():
        gate = asyncio.Event()
        async with running(tmp_path, Upstream(gate, DONE)) as (relay, client):
            run = (await post_run(client)).json()
            follower = relay.follow(run["id"])
            assert await anext(follower) == ": ping\n\n"
            gate.set()
            assert [e async for e in follower if not e.startswith(":")]

    go(main())


def test_old_finished_runs_are_purged(tmp_path):
    store = Store(tmp_path / "relay.db")
    store.create("r_old", "c_old", "w", "t", "chat", "q")
    store.append("r_old", *DONE)
    store.create("r_live", "c_live", "w", "t2", "chat", "q")
    later = datetime.now(UTC) + timedelta(days=8)
    assert store.purge(7, now=later) == 1
    assert store.get("r_old") is None and store.events("r_old") == []
    assert store.get("r_live") is not None  # still running, so kept
