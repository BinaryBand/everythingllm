"""The handoff's acceptance list, against a scripted AnythingLLM: `Upstream` stands in for
relay.upstream.answer (its events, when it's closed and how often it's called), and
test_upstream.py covers the real one's parsing of stream-chat."""

import asyncio
import json
import logging

import httpx
import pytest
from relay.app import Config, create_app
from relay.runs import RESTARTED, Relay
from relay.store import Store

from relay import upstream

KEY = "allm-key-0123456789"
TOKEN = "relay-token-abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BODY = {
    "workspace": "planning",
    "thread": "t1",
    "message": "What's next?",
    "mode": "query",
    "clientId": "c_1",
}


def go(coro):
    return asyncio.run(asyncio.wait_for(coro, 10))


class Upstream:
    """A scripted answer: yields `script` (a list of events, or an asyncio.Event to wait
    on) and records whether the relay closed it early."""

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


def setup(tmp_path, answer, notify=None):
    store = Store(tmp_path / "relay.db")
    relay = Relay(store, answer, notify, ping=0.05)
    app = create_app(Config(api_key=KEY, token=TOKEN), relay)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://relay"
    )
    return relay, client


async def finished(relay, run_id):
    while relay.store.get(run_id)["status"] == "running":
        await asyncio.sleep(0.01)


# 1
def test_a_run_streams_its_pieces_then_done_with_the_citations(tmp_path):
    answer = Upstream(text("Hel"), text("lo"), ("done", {"citations": ["Doc A"]}))
    notified = []

    async def notify(run, question):
        notified.append((run["status"], question))

    async def main():
        relay, client = setup(tmp_path, answer, notify)
        async with relay, client:
            r = await client.post("/runs", json=BODY, headers=AUTH)
            assert r.status_code == 201
            run = r.json()
            assert run["id"].startswith("r_") and run["clientId"] == "c_1"
            assert (run["status"], run["finishedAt"]) == ("running", None)
            await finished(relay, run["id"])
            r = await client.get(f"/runs/{run['id']}/events", headers=AUTH)
            assert r.headers["content-type"].startswith("text/event-stream")
            assert parse(r.text) == [
                (1, "text", {"text": "Hel"}),
                (2, "text", {"text": "lo"}),
                (3, "done", {"citations": ["Doc A"]}),
            ]
            got = (await client.get(f"/runs/{run['id']}", headers=AUTH)).json()
            assert got["status"] == "done" and got["finishedAt"]

    go(main())
    assert notified == [("done", "What's next?")]


# 2
def test_a_follower_leaving_doesnt_stop_the_answer(tmp_path):
    async def main():
        gate = asyncio.Event()
        answer = Upstream(text("one"), gate, text("two"), ("done", {"citations": []}))
        relay, client = setup(tmp_path, answer)
        async with relay, client:
            run = (await client.post("/runs", json=BODY, headers=AUTH)).json()
            follower = relay.follow(run["id"])
            assert parse(await anext(follower)) == [(1, "text", {"text": "one"})]
            await follower.aclose()  # the app goes away mid-answer
            await asyncio.sleep(0.1)
            assert (
                not answer.closed and relay.store.get(run["id"])["status"] == "running"
            )
            gate.set()
            await finished(relay, run["id"])
            assert answer.finished and not answer.closed
            assert [e[1] for e in relay.store.events(run["id"])] == [
                "text",
                "text",
                "done",
            ]

    go(main())


# 3
def test_rejoining_with_last_event_id_gets_exactly_what_came_after(tmp_path):
    async def main():
        gate = asyncio.Event()
        answer = Upstream(
            text("a"), text("b"), gate, text("c"), ("done", {"citations": []})
        )
        relay, client = setup(tmp_path, answer)
        async with relay, client:
            run = (await client.post("/runs", json=BODY, headers=AUTH)).json()
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
                f"/runs/{run['id']}/events", headers={**AUTH, "Last-Event-ID": "2"}
            )
            assert [e[:2] for e in parse(r.text)] == [(3, "text"), (4, "done")]
            r = await client.get(f"/runs/{run['id']}/events", headers=AUTH)
            assert [e[0] for e in parse(r.text)] == [1, 2, 3, 4]

    go(main())


# 4
def test_the_same_client_id_returns_the_same_run_and_calls_upstream_once(tmp_path):
    answer = Upstream(text("x"), ("done", {"citations": []}))

    async def main():
        relay, client = setup(tmp_path, answer)
        async with relay, client:
            first = await client.post("/runs", json=BODY, headers=AUTH)
            again = await client.post("/runs", json=BODY, headers=AUTH)
            assert (first.status_code, again.status_code) == (201, 200)
            assert first.json()["id"] == again.json()["id"]
            await finished(relay, first.json()["id"])
            # Even after it ended, and even on another thread.
            later = await client.post(
                "/runs", json={**BODY, "thread": "t2"}, headers=AUTH
            )
            assert later.status_code == 200 and later.json()["id"] == first.json()["id"]

    go(main())
    assert answer.calls == 1


# 5
def test_a_thread_takes_one_running_run_at_a_time(tmp_path):
    async def main():
        gate = asyncio.Event()
        relay, client = setup(tmp_path, Upstream(gate, ("done", {"citations": []})))
        async with relay, client:
            first = await client.post("/runs", json=BODY, headers=AUTH)
            second = await client.post(
                "/runs", json={**BODY, "clientId": "c_2"}, headers=AUTH
            )
            assert second.status_code == 409 and "error" in second.json()
            other = await client.post(
                "/runs", json={**BODY, "clientId": "c_3", "thread": "t2"}, headers=AUTH
            )
            assert other.status_code == 201
            gate.set()
            await finished(relay, first.json()["id"])
            again = await client.post(
                "/runs", json={**BODY, "clientId": "c_4"}, headers=AUTH
            )
            assert again.status_code == 201

    go(main())


# 6
def test_cancel_closes_the_connection_and_ends_with_cancelled(tmp_path):
    async def main():
        gate = asyncio.Event()
        answer = Upstream(text("so far"), gate, text("never"))
        relay, client = setup(tmp_path, answer)
        async with relay, client:
            run = (await client.post("/runs", json=BODY, headers=AUTH)).json()
            follower = relay.follow(run["id"])
            await anext(follower)
            r = await client.post(f"/runs/{run['id']}/cancel", headers=AUTH)
            assert r.status_code == 200 and r.json()["status"] == "cancelled"
            assert answer.closed
            rest = [e async for e in follower if not e.startswith(":")]
            assert parse("".join(rest)) == [(2, "cancelled", {})]
            # On a run that has ended, cancel changes nothing.
            again = await client.post(f"/runs/{run['id']}/cancel", headers=AUTH)
            assert again.status_code == 200 and again.json() == r.json()
            assert len(relay.store.events(run["id"])) == 2
            missing = await client.post("/runs/r_nope/cancel", headers=AUTH)
            assert missing.status_code == 404

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
    notified = []

    async def notify(run, question):
        notified.append(run["status"])

    async def main():
        relay, client = setup(tmp_path, Upstream(text("pa"), event), notify)
        async with relay, client:
            run = (await client.post("/runs", json=BODY, headers=AUTH)).json()
            await finished(relay, run["id"])
            r = await client.get(f"/runs/{run['id']}/events", headers=AUTH)
            assert parse(r.text)[-1] == (2, "failed", event[1])
            assert relay.store.get(run["id"])["status"] == "failed"

    go(main())
    assert notified == ["failed"]


def test_a_stream_that_ends_without_close_is_done(tmp_path):
    async def main():
        relay, client = setup(tmp_path, Upstream(text("all")))
        async with relay, client:
            run = (await client.post("/runs", json=BODY, headers=AUTH)).json()
            await finished(relay, run["id"])
            assert relay.store.events(run["id"])[-1] == (2, "done", {"citations": []})

    go(main())


# 8
def test_a_restart_mid_run_fails_it_and_keeps_its_events(tmp_path):
    async def first_life():
        gate = asyncio.Event()
        relay, client = setup(tmp_path, Upstream(text("partial"), gate))
        async with relay, client:
            run = (await client.post("/runs", json=BODY, headers=AUTH)).json()
            while not relay.store.events(run["id"]):
                await asyncio.sleep(0.01)
        relay.store.close()  # the relay stops mid-answer
        return run["id"]

    run_id = go(first_life())

    async def second_life():
        relay, client = setup(tmp_path, Upstream())
        async with relay, client:
            run = (await client.get(f"/runs/{run_id}", headers=AUTH)).json()
            assert run["status"] == "failed" and run["finishedAt"]
            r = await client.get(f"/runs/{run_id}/events", headers=AUTH)
            assert parse(r.text) == [
                (1, "text", {"text": "partial"}),
                (2, "failed", {"error": RESTARTED}),
            ]
            running = await client.get("/runs?status=running", headers=AUTH)
            assert running.json() == []

    go(second_life())


# 9
def test_no_body_or_log_line_carries_the_key_or_the_token(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    def answer_401(workspace, thread, question, mode):
        transport = httpx.MockTransport(lambda request: httpx.Response(401))
        client = httpx.AsyncClient(transport=transport)
        return upstream.answer(
            client, "http://allm", KEY, workspace, thread, question, mode
        )

    bodies = []

    async def main():
        relay, client = setup(tmp_path, answer_401)
        async with relay, client:
            for r in [
                await client.post("/runs", json=BODY, headers=AUTH),
                await client.post("/runs", json=BODY),
                await client.post("/runs", json={"nope": 1}, headers=AUTH),
                await client.get(
                    "/runs/r_x", headers={"Authorization": "Bearer wrong"}
                ),
            ]:
                bodies.append(r.text)
            run_id = json.loads(bodies[0])["id"]
            await finished(relay, run_id)
            for path in (f"/runs/{run_id}", f"/runs/{run_id}/events", "/runs"):
                bodies.append((await client.get(path, headers=AUTH)).text)

    go(main())
    assert upstream.status_error(401) in bodies[5]  # it did fail, with the message
    for text_ in [*bodies, caplog.text]:
        assert KEY not in text_ and TOKEN not in text_


# --- the rest of the API ---


def test_every_route_but_health_needs_the_token(tmp_path):
    async def main():
        relay, client = setup(tmp_path, Upstream())
        async with relay, client:
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
        relay, client = setup(tmp_path, Upstream(gate, ("done", {"citations": []})))
        async with relay, client:
            a = (await client.post("/runs", json=BODY, headers=AUTH)).json()
            b = (
                await client.post(
                    "/runs",
                    json={**BODY, "clientId": "c_2", "thread": "t2"},
                    headers=AUTH,
                )
            ).json()
            running = (await client.get("/runs?status=running", headers=AUTH)).json()
            assert [r["id"] for r in running] == [a["id"], b["id"]]
            assert (
                await client.get("/runs?status=odd", headers=AUTH)
            ).status_code == 400
            gate.set()
            await finished(relay, a["id"])
            await finished(relay, b["id"])
            assert (await client.get("/runs?status=running", headers=AUTH)).json() == []
            assert len((await client.get("/runs", headers=AUTH)).json()) == 2
            for path in ("/runs/r_nope", "/runs/r_nope/events"):
                r = await client.get(path, headers=AUTH)
                assert r.status_code == 404 and r.json() == {"error": "No such run."}
            for body in ({**BODY, "message": " "}, {**BODY, "mode": "agent"}, ["x"]):
                r = await client.post("/runs", json=body, headers=AUTH)
                assert r.status_code == 400 and "error" in r.json()
            r = await client.post("/runs", content=b"{", headers=AUTH)
            assert r.status_code == 400

    go(main())


def test_a_quiet_run_is_pinged(tmp_path):
    async def main():
        gate = asyncio.Event()
        relay, client = setup(tmp_path, Upstream(gate, ("done", {"citations": []})))
        async with relay, client:
            run = (await client.post("/runs", json=BODY, headers=AUTH)).json()
            follower = relay.follow(run["id"])
            assert await anext(follower) == ": ping\n\n"
            gate.set()
            assert [e async for e in follower if not e.startswith(":")]

    go(main())


def test_old_finished_runs_are_purged(tmp_path):
    from datetime import UTC, datetime, timedelta

    store = Store(tmp_path / "relay.db")
    store.create("r_old", "c_old", "w", "t", "chat", "q")
    store.append("r_old", "done", {"citations": []})
    store.create("r_live", "c_live", "w", "t2", "chat", "q")
    later = datetime.now(UTC) + timedelta(days=8)
    assert store.purge(7, now=later) == 1
    assert store.get("r_old") is None and store.events("r_old") == []
    assert store.get("r_live") is not None  # still running, so kept
