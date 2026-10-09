"""Access requests (sandbox.accessreq): a chat that can't ask the user leaves one, and a client
with the user's developer API key answers it through the apps server (sandbox.appsweb)."""

import asyncio
import json

import pytest
from hostenv.keys import REFUSED
from sandbox import accessreq, appsweb, runner
from test_runner import GATEWAY, A, B, cfg, go, make  # noqa: F401 - cfg is a fixture

TEXT = "Let this workspace's code runs reach public websites."


@pytest.fixture
def r(cfg):  # noqa: F811
    return make(cfg)


async def check(key: str):
    return None if key == "KEY" else (403, REFUSED)


async def served(r):
    server = await appsweb.AppsWeb(r, check).serve(0)
    return server, server.sockets[0].getsockname()[1]


async def call(port, method: str, path: str, key: str | None = "KEY"):
    head = f"{method} {path} HTTP/1.1\r\nHost: x\r\n"
    if key is not None:
        head += f"Authorization: Bearer {key}\r\n"
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head.encode() + b"\r\n")
    await writer.drain()
    data = await reader.read()
    writer.close()
    top, _, payload = data.partition(b"\r\n\r\n")
    return int(top.split()[1]), json.loads(payload or b"{}"), top


def ask(r, scope=A, **change):
    return go(r.op_access_ask(scope, TEXT, **change))


def test_an_api_chat_leaves_a_request_and_nothing_changes(r, cfg):  # noqa: F811
    one = ask(r, web=True)
    assert one["state"] == "waiting" and one["text"] == TEXT
    assert one["would"] == {
        "web": True,
        "models": False,
        "daily_tokens": 200_000,
        "heavy": False,
    }
    assert one["id"].startswith("ar-") and one["workspace"] == "career"
    assert not cfg.access_file.exists()
    assert ask(r, A, web=True)["id"] == one["id"]  # the same change asks once
    assert ask(r, heavy=True)["id"] != one["id"]


def test_what_cant_be_asked_for(r):
    with pytest.raises(runner.SandboxError, match="gateway client"):
        ask(r, GATEWAY, web=True)
    with pytest.raises(runner.SandboxError, match="scheduled job"):
        ask(r, {"workspace": "_jobs", "thread": "default"}, web=True)
    with pytest.raises(runner.SandboxError, match="needs no approval"):
        ask(r, web=False)
    with pytest.raises(runner.SandboxError, match="web must be"):
        ask(r, web="on")
    with pytest.raises(runner.SandboxError, match="what the user is asked"):
        go(r.op_access_ask(A, " ", web=True))
    for i in range(accessreq.MAX_WAITING):
        ask(r, daily_tokens=300_000 + i, models=True)
    with pytest.raises(runner.SandboxError, match="too many access requests"):
        ask(r, daily_tokens=999_999, models=True)
    assert ask(r, B, web=True)["workspace"] == "home"  # per workspace


def test_a_client_with_the_key_lists_and_approves(r, cfg):  # noqa: F811
    one = ask(r, web=True)

    async def main():
        server, port = await served(r)
        try:
            status, body, top = await call(port, "GET", "/_live/sandbox/access/career")
            assert status == 200
            assert b"Access-Control-Allow-Origin: *" in top
            assert body["access"]["web"] is False
            assert [q["id"] for q in body["requests"]] == [one["id"]]
            assert body["requests"][0]["state"] == "waiting"
            path = f"/_live/sandbox/access/career/{one['id']}/approve"
            status, body, _ = await call(port, "POST", path)
            assert status == 200 and body["state"] == "approved"
            assert json.loads(cfg.access_file.read_text())["career"]["web"] is True
            status, body, _ = await call(port, "POST", path)
            assert status == 409 and "approved" in body["error"]
            status, body, _ = await call(port, "GET", "/_live/sandbox/access/career")
            assert body["access"]["web"] is True
            assert body["requests"][0]["state"] == "approved"
        finally:
            server.close()

    asyncio.run(main())
    assert go(r.op_access(A))["web"] is True


def test_declining_changes_nothing(r, cfg):  # noqa: F811
    one = ask(r, web=True)

    async def main():
        server, port = await served(r)
        try:
            path = f"/_live/sandbox/access/career/{one['id']}/decline"
            status, body, _ = await call(port, "POST", path)
            assert status == 200 and body["state"] == "declined"
        finally:
            server.close()

    asyncio.run(main())
    assert not cfg.access_file.exists()


def test_only_a_good_key_answers(r, cfg):  # noqa: F811
    one = ask(r, web=True)
    path = f"/_live/sandbox/access/career/{one['id']}/approve"

    async def main():
        server, port = await served(r)
        try:
            for key in (None, "", "wrong"):
                status, body, _ = await call(port, "POST", path, key)
                assert status in (401, 403) and body["error"] == REFUSED
                status, _, _ = await call(
                    port, "GET", "/_live/sandbox/access/career", key
                )
                assert status in (401, 403)
            # Another workspace's request isn't this one's to answer, nor a made-up id.
            other = f"/_live/sandbox/access/home/{one['id']}/approve"
            assert (await call(port, "POST", other))[0] == 404
            made_up = f"/_live/sandbox/access/career/ar-{'0' * 32}/approve"
            assert (await call(port, "POST", made_up))[0] == 404
            assert (await call(port, "GET", path))[0] == 405
            assert (await call(port, "POST", "/_live/sandbox/access/career"))[0] == 405
        finally:
            server.close()

    asyncio.run(main())
    assert not cfg.access_file.exists()


def test_without_the_prefix_only_a_key_makes_it_the_access_route(r):
    async def main():
        server, port = await served(r)
        try:
            # the machine's route strips the prefix: a visit with no key is an app's page
            status, _, top = await call(port, "GET", "/access/career", key=None)
            assert status == 302 and b"/career/apps/" not in top
            assert (await call(port, "GET", "/access/career"))[0] == 200
        finally:
            server.close()

    asyncio.run(main())


def test_a_request_expires_and_is_forgotten(r, cfg):  # noqa: F811
    now = [1000.0]
    r.access_requests.now = lambda: now[0]
    one = ask(r, web=True)
    now[0] += accessreq.ASK_SECONDS + 1

    async def main():
        server, port = await served(r)
        try:
            status, body, _ = await call(port, "GET", "/_live/sandbox/access/career")
            assert body["requests"][0]["state"] == "expired"
            path = f"/_live/sandbox/access/career/{one['id']}/approve"
            status, body, _ = await call(port, "POST", path)
            assert status == 409 and "expired" in body["error"]
        finally:
            server.close()

    asyncio.run(main())
    assert not cfg.access_file.exists()
    assert ask(r, web=True)["id"] != one["id"]  # an expired one isn't reused
    now[0] += accessreq.KEEP_ASKED + accessreq.ASK_SECONDS + 1
    ask(r, B, web=True)  # asking forgets what is long answered
    assert r.access_requests.listing("career") == []


def test_approval_applies_the_change_to_the_access_as_it_is_now(r, cfg):  # noqa: F811
    one = ask(r, web=True)
    go(r.op_access(A, heavy=True, apply=True, approved=True))  # changed meanwhile
    done = go(r.answer_access("career", one["id"], True))
    assert done["state"] == "approved"
    got = go(r.op_access(A))
    assert got["web"] is True and got["heavy"] is True
