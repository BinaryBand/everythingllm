"""The handoff's acceptance list, against a scripted AnythingLLM: `Upstream` stands in for
relay.upstream.answer (its events, when it's closed and how often it's called), `Allm` for
the key check, and test_upstream.py covers the real answer's parsing of stream-chat. Each
test runs the app's own lifespan, as uvicorn would."""

import asyncio
import base64
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from relay import upstream
from relay.app import HEALTH, Config, create_app
from relay.auth import REFUSED
from relay.runs import RESET, RESTARTED
from relay.store import VERSION, Store

KEY = "allm-key-0123456789"
CHAT = {"message": "What's next?"}
BODY = {"workspace": "planning", "thread": "t1", "clientId": "c_1", "body": CHAT}
DONE = ("done", {})


def go(coro):
    return asyncio.run(asyncio.wait_for(coro, 10))


class Upstream:
    """A scripted answer: yields `script` (events, or an asyncio.Event to wait on) and
    records the bodies it was called with and whether the relay closed it early."""

    def __init__(self, *script):
        self.script = list(script)
        self.bodies = []
        self.keys = []
        self.closed = False
        self.finished = False

    @property
    def calls(self):
        return len(self.bodies)

    async def __call__(self, workspace, thread, body, api_key):
        self.bodies.append(body)
        self.keys.append(api_key)
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
    """A chat answer's piece, as stream-chat sends it and the relay hands it back."""
    return ("chunk", {"type": "textResponseChunk", "textResponse": t, "close": False})


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


class Allm(list):
    """AnythingLLM's GET /api/v1/auth, which takes KEY: records the keys it was asked
    about, and raises `error` instead of answering when it's set."""

    error = None

    def __call__(self, request):
        assert request.url.path == "/api/v1/auth"
        self.append(request.headers.get("authorization"))
        if self.error:
            raise self.error
        if request.headers.get("authorization") == f"Bearer {KEY}":
            return httpx.Response(200, json={"authenticated": True})
        return httpx.Response(403, json={"error": REFUSED})


@contextlib.asynccontextmanager
async def running(tmp_path, answer, notify_finished=None, allm=None):
    """The app started as uvicorn would, and a client that sends AnythingLLM's key."""
    app = create_app(
        Config(database=tmp_path / "relay.db"),
        answer,
        notify_finished,
        httpx.AsyncClient(
            transport=httpx.MockTransport(Allm() if allm is None else allm)
        ),
    )
    relay = app.state.relay
    relay.ping = 0.05
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://relay",
            headers={"Authorization": f"Bearer {KEY}"},
        ) as client,
    ):
        yield relay, client


async def finished(relay, run_id):
    while relay.store.get(run_id)["status"] == "running":
        await asyncio.sleep(0.01)


async def post_run(client, **changes):
    return await client.post("/v1/runs", json={**BODY, **changes})


# 1
def test_a_run_streams_its_chunks_then_done(tmp_path):
    close = (
        "chunk",
        {
            "type": "textResponseChunk",
            "close": True,
            "sources": [{"title": "A", "chunkSource": "link://https://a.example/"}],
        },
    )
    answer = Upstream(text("Hel"), text("lo"), close, DONE)
    notified = Notified()

    async def main():
        async with running(tmp_path, answer, notified) as (relay, client):
            r = await post_run(client)
            assert r.status_code == 201
            run = r.json()
            assert run["id"].startswith("r_") and run["clientId"] == "c_1"
            assert (run["status"], run["finishedAt"], run["mode"]) == (
                "running",
                None,
                None,
            )
            await finished(relay, run["id"])
            r = await client.get(f"/v1/runs/{run['id']}/events")
            assert r.headers["content-type"].startswith("text/event-stream")
            assert parse(r.text) == [
                (1, *text("Hel")),
                (2, *text("lo")),
                (3, *close),
                (4, *DONE),
            ]
            got = (await client.get(f"/v1/runs/{run['id']}")).json()
            assert got["status"] == "done" and got["finishedAt"]

    go(main())
    assert answer.bodies == [CHAT]
    assert notified == [("done", "What's next?")]


# 2
def test_a_follower_leaving_doesnt_stop_the_answer(tmp_path):
    async def main():
        gate = asyncio.Event()
        answer = Upstream(text("one"), gate, text("two"), DONE)
        async with running(tmp_path, answer) as (relay, client):
            run = (await post_run(client)).json()
            follower = relay.follow(run["id"])
            assert parse(await anext(follower)) == [(1, *text("one"))]
            await follower.aclose()  # the app goes away mid-answer
            await asyncio.sleep(0.1)
            assert not answer.closed
            assert relay.store.get(run["id"])["status"] == "running"
            gate.set()
            await finished(relay, run["id"])
            assert answer.finished and not answer.closed
            events = relay.store.events(run["id"])
            assert [e[1] for e in events] == ["chunk", "chunk", "done"]

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
            assert parse(await anext(follower)) == [(2, *text("b"))]
            gate.set()
            rest = [e async for e in follower if not e.startswith(":")]
            assert parse("".join(rest)) == [(3, *text("c")), (4, *DONE)]
            # Finished: through the API.
            r = await client.get(
                f"/v1/runs/{run['id']}/events", headers={"Last-Event-ID": "2"}
            )
            assert parse(r.text) == [(3, *text("c")), (4, *DONE)]
            r = await client.get(f"/v1/runs/{run['id']}/events")
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
            r = await client.post(f"/v1/runs/{run['id']}/cancel")
            assert r.status_code == 200 and r.json()["status"] == "cancelled"
            assert answer.closed
            rest = [e async for e in follower if not e.startswith(":")]
            assert parse("".join(rest)) == [(2, "cancelled", {})]
            # On a run that has ended, cancel changes nothing.
            again = await client.post(f"/v1/runs/{run['id']}/cancel")
            assert again.status_code == 200 and again.json() == r.json()
            assert len(relay.store.events(run["id"])) == 2
            assert (await client.post("/v1/runs/r_nope/cancel")).status_code == 404

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
            r = await client.get(f"/v1/runs/{run['id']}/events")
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
            run = (await client.get(f"/v1/runs/{run_id}")).json()
            assert run["status"] == "failed" and run["finishedAt"]
            r = await client.get(f"/v1/runs/{run_id}/events")
            assert parse(r.text) == [
                (1, *text("partial")),
                (2, "failed", {"error": RESTARTED}),
            ]
            assert (await client.get("/v1/runs?status=running")).json() == []

    go(second_life())


# 9
def test_no_body_or_log_line_carries_the_key(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    transport = httpx.MockTransport(lambda request: httpx.Response(401))
    allm = httpx.AsyncClient(transport=transport)

    def answer_401(workspace, thread, body, api_key):
        return upstream.answer(allm, "http://allm", workspace, thread, body, api_key)

    bodies = []

    async def main():
        async with running(tmp_path, answer_401) as (relay, client):
            run = await post_run(client)
            bodies.append(run.text)
            no_token = {"Authorization": ""}
            for r in [
                await client.post("/v1/runs", json=BODY, headers=no_token),
                await client.post("/v1/runs", json={"nope": 1}),
                await client.get(
                    "/v1/runs/r_x", headers={"Authorization": "Bearer wrong"}
                ),
            ]:
                bodies.append(r.text)
            run_id = run.json()["id"]
            await finished(relay, run_id)
            for path in (f"/v1/runs/{run_id}", f"/v1/runs/{run_id}/events", "/v1/runs"):
                bodies.append((await client.get(path)).text)
        await allm.aclose()

    go(main())
    assert upstream.status_error(401) in bodies[5]  # it did fail, with the message
    for text_ in [*bodies, caplog.text]:
        assert KEY not in text_


# --- the rest of the API ---


def test_every_route_but_health_needs_a_key_anythingllm_takes(tmp_path):
    async def main():
        async with running(tmp_path, Upstream()) as (_, client):
            del client.headers["Authorization"]
            assert (await client.get("/health")).json() == HEALTH
            for method, path in [
                ("GET", "/v1/runs"),
                ("POST", "/v1/runs"),
                ("GET", "/v1/runs/r_x"),
                ("GET", "/v1/runs/r_x/events"),
                ("POST", "/v1/runs/r_x/cancel"),
            ]:
                for headers in (
                    {},
                    {"Authorization": "Bearer "},
                    {"Authorization": "Bearer nope"},
                    {"Authorization": KEY},
                    {"Authorization": f"Basic {KEY}"},
                ):
                    r = await client.request(method, path, headers=headers)
                    assert r.status_code == 403, (method, path, headers)
                    assert r.json() == {"error": REFUSED}  # as AnythingLLM says it

    go(main())


def test_the_routes_answer_under_everythingllm_too(tmp_path):
    answer = Upstream(DONE)

    async def main():
        async with running(tmp_path, answer) as (relay, client):
            health = await client.get("/everythingllm/health", headers={})
            assert health.json() == HEALTH
            r = await client.post("/everythingllm/v1/runs", json=BODY)
            assert r.status_code == 201
            await finished(relay, r.json()["id"])
            got = await client.get(f"/everythingllm/v1/runs/{r.json()['id']}")
            assert got.json()["status"] == "done"
            del client.headers["Authorization"]
            assert (await client.get("/everythingllm/v1/runs")).status_code == 403
            assert (await client.get("/everythingllmx/v1/runs")).status_code == 403

    go(main())


def test_a_run_asks_anythingllm_with_the_callers_key(tmp_path):
    answer = Upstream(DONE)

    async def main():
        async with running(tmp_path, answer) as (relay, client):
            r = await post_run(client)
            await finished(relay, r.json()["id"])

    go(main())
    assert answer.keys == [KEY]


def test_a_good_key_is_checked_once_a_minute_and_a_bad_one_every_time(tmp_path):
    allm = Allm()

    async def main():
        async with running(tmp_path, Upstream(), allm=allm) as (_, client):
            for _ in range(3):
                assert (await client.get("/v1/runs")).status_code == 200
            bad = {"Authorization": "Bearer nope"}
            for _ in range(2):
                assert (await client.get("/v1/runs", headers=bad)).status_code == 403

    go(main())
    assert allm == [f"Bearer {KEY}", "Bearer nope", "Bearer nope"]


def test_an_unreachable_anythingllm_answers_502(tmp_path):
    allm = Allm()
    allm.error = httpx.ConnectError("refused")

    async def main():
        async with running(tmp_path, Upstream(), allm=allm) as (_, client):
            r = await client.get("/v1/runs")
            assert r.status_code == 502
            assert r.json() == {"error": upstream.UNREACHABLE}

    go(main())


def test_listing_unknown_runs_and_bad_bodies(tmp_path):
    async def main():
        gate = asyncio.Event()
        async with running(tmp_path, Upstream(gate, DONE)) as (relay, client):
            a = (await post_run(client)).json()
            b = (await post_run(client, clientId="c_2", thread="t2")).json()
            running_ = (await client.get("/v1/runs?status=running")).json()
            assert [r["id"] for r in running_] == [a["id"], b["id"]]
            assert (await client.get("/v1/runs?status=odd")).status_code == 400
            gate.set()
            await finished(relay, a["id"])
            await finished(relay, b["id"])
            assert (await client.get("/v1/runs?status=running")).json() == []
            assert len((await client.get("/v1/runs")).json()) == 2
            for path in ("/v1/runs/r_nope", "/v1/runs/r_nope/events"):
                r = await client.get(path)
                assert r.status_code == 404 and r.json() == {"error": "No such run."}
            for body in (
                {**BODY, "workspace": " "},
                {**BODY, "body": {"message": " "}},
                {**BODY, "body": {"mode": "chat"}},
                {**BODY, "body": {"reset": "yes"}},
                {**BODY, "body": "What's next?"},
                {k: v for k, v in BODY.items() if k != "body"},
                ["x"],
            ):
                r = await client.post("/v1/runs", json=body)
                assert r.status_code == 400 and "error" in r.json(), body
            old = {k: v for k, v in BODY.items() if k != "body"}
            r = await client.post("/v1/runs", json={**old, **CHAT, "mode": "chat"})
            assert r.status_code == 400 and "'body'" in r.json()["error"]
            assert (await client.post("/v1/runs", content=b"{")).status_code == 400

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


# --- the body ---


def test_a_reset_runs_and_isnt_notified(tmp_path):
    answer = Upstream(("chunk", {"type": "textResponse", "close": True}), DONE)
    notified = Notified()

    async def main():
        async with running(tmp_path, answer, notified) as (relay, client):
            r = await post_run(client, body={"reset": True})
            assert r.status_code == 201
            await finished(relay, r.json()["id"])
            row = relay.store.get(r.json()["id"])
            assert (row["status"], row["question"]) == ("done", RESET)

    go(main())
    assert answer.bodies == [{"reset": True}]
    assert notified == []


def test_the_body_reaches_upstream_whole_with_its_mode_and_a_20_mb_attachment(
    tmp_path,
):
    content = base64.b64encode(b"\x89" * 20 * 1024 * 1024).decode()
    chat = {
        "message": "Read this",
        "mode": "query",
        "attachments": [
            {
                "name": "big.pdf",
                "mime": "application/anythingllm-document",
                "contentString": f"data:application/pdf;base64,{content}",
            }
        ],
    }
    answer = Upstream(DONE)

    async def main():
        async with running(tmp_path, answer) as (relay, client):
            r = await post_run(client, body=chat)
            assert r.status_code == 201 and r.json()["mode"] == "query"
            await finished(relay, r.json()["id"])

    go(main())
    assert answer.bodies == [chat]
    assert content not in (tmp_path / "relay.db").read_bytes().decode(errors="replace")


def test_a_database_from_before_version_2_loses_its_runs(tmp_path):
    path = tmp_path / "relay.db"
    store = Store(path)
    store.create("r_1", "c_1", "w", "t", "chat", "q")
    store.append("r_1", "done", {"citations": []})
    store.db.execute("pragma user_version = 1")
    store.close()

    store = Store(path)
    assert store.runs() == []
    assert store.db.execute("pragma user_version").fetchone()[0] == VERSION
    store.create("r_2", "c_2", "w", "t", None, "q")
    store.close()

    store = Store(path)  # at the current version, runs are kept
    assert [r["id"] for r in store.runs()] == ["r_2"]
    store.close()
