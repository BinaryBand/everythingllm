"""The take-over view (browser.takeover): its page and files, taking the browser and
handing it back from the page's own origin only, and the WebSocket carried to x11vnc."""

import asyncio
import json
import os

from browser import takeover, websocket
from browser.runner import Runner
from browser_fakes import Clock, FakePodman, config, passkey, scope

HOST = "host.example.ts.net:8454"


async def request(port, method, path, headers=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = [f"{method} {path} HTTP/1.1", f"Host: {HOST}"]
    lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await writer.drain()
    return reader, writer


async def answer(port, method, path, headers=None):
    reader, _ = await request(port, method, path, headers)
    head, _, body = (await reader.read()).partition(b"\r\n\r\n")
    return head.decode(), body


def with_view(test):
    def go(tmp_path):
        async def main():
            podman = FakePodman()
            r = Runner(config(tmp_path), podman=podman, now=Clock())
            server = await takeover.Takeover(r).serve(0)
            try:
                await r.op_open(scope(), "https://example.com/")
                await test(r, server.sockets[0].getsockname()[1], podman, tmp_path)
            finally:
                server.close()
                await podman.close()

        asyncio.run(main())

    return go


def test_the_page_its_files_and_its_state(tmp_path):
    @with_view
    async def test(r, port, podman, tmp_path):
        s = r.sessions["career"]
        tab = r.threads[("career", "7")]
        head, body = await answer(port, "GET", "/health")
        assert "200 OK" in head and body == b"ok\n"
        head, _ = await answer(port, "GET", "/not-the-token/")
        assert "404" in head
        head, _ = await answer(port, "GET", f"/{s.token}")
        assert "302" in head and f"Location: /{s.token}/" in head
        head, body = await answer(port, "GET", f"/{s.token}/?tab={tab.id}")
        assert "200 OK" in head and f"Content-Security-Policy: {takeover.CSP}" in head
        assert b"Browser \xc2\xb7 career" in body and b'src="app.js"' in body
        assert b'id="problem"' in body  # where a button's failure shows
        assert b'<button id="fit"' in body  # a phone's view pans at full size
        # A field to type into the browser as text, and one a password manager fills.
        assert b'<form id="typing" hidden>' in body and b'<button id="keyboard"' in body
        assert b'autocomplete="current-password"' in body
        assert ("front", {"thread": "7"}) in podman.drivers[s.name].calls
        head, body = await answer(port, "GET", f"/{s.token}/app.js")
        assert "text/javascript" in head and b"novnc/core/rfb.js" in body
        novnc = r.config.data / "novnc" / "core"
        novnc.mkdir(parents=True)
        (novnc / "rfb.js").write_text("export default 1;")
        (r.config.data / "secret.js").write_text("no")
        head, body = await answer(port, "GET", f"/{s.token}/novnc/core/rfb.js")
        assert "200 OK" in head and body == b"export default 1;"
        for bad in (
            "novnc/../secret.js",
            "novnc/core/../../secret.js",
            "novnc/core/missing.js",
        ):
            assert "404" in (await answer(port, "GET", f"/{s.token}/{bad}"))[0], bad
        os.symlink(r.config.data / "secret.js", novnc / "link.js")
        assert "404" in (await answer(port, "GET", f"/{s.token}/novnc/core/link.js"))[0]
        head, body = await answer(port, "GET", f"/{s.token}/state")
        state = json.loads(body)
        assert state["control"] == "agent" and state["tabs"][0]["id"] == tab.id
        assert state["state"] == "idle"  # opened outside an op the agent sent

    test(tmp_path)


def test_taking_and_handing_back_need_the_pages_own_origin(tmp_path):
    @with_view
    async def test(r, port, podman, tmp_path):
        s = r.sessions["career"]
        for origin in (
            {},
            {"Origin": "https://evil.example"},
            {"Origin": "https://host.example.ts.net:8445"},
        ):
            head, _ = await answer(port, "POST", f"/{s.token}/take", origin)
            assert "403" in head, origin
        assert s.control == "agent"
        mine = {"Origin": f"https://{HOST}"}
        head, body = await answer(port, "POST", f"/{s.token}/take", mine)
        assert json.loads(body)["control"] == "user" and s.control == "user"
        await r.op_handoff(scope(), "log in")
        state = json.loads((await answer(port, "GET", f"/{s.token}/state"))[1])
        assert state["waiting"] and state["reason"] == "log in"
        head, body = await answer(port, "POST", f"/{s.token}/give", mine)
        assert json.loads(body)["control"] == "agent" and not s.asked
        assert (await r.op_act(scope(), "click", "e1"))["page"]

    test(tmp_path)


def test_the_websocket_is_carried_to_the_browsers_screen(tmp_path):
    @with_view
    async def test(r, port, podman, tmp_path):
        s = r.sessions["career"]
        got = []

        async def vnc(reader, writer):  # a fake x11vnc: greets, then echoes
            writer.write(b"RFB 003.008\n")
            while data := await reader.read(65536):
                got.append(data)
                writer.write(data)
                await writer.drain()

        cwd = os.getcwd()
        os.chdir(s.folder)  # tmp_path can be too long for a socket's path
        try:
            server = await asyncio.start_unix_server(vnc, "vnc.sock")
        finally:
            os.chdir(cwd)
        upgrade = {
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
            "Sec-WebSocket-Protocol": "binary",
            "Sec-WebSocket-Version": "13",
        }
        head, _ = await answer(port, "GET", f"/{s.token}/websockify", upgrade)
        assert "403" in head  # no Origin
        reader, writer = await request(
            port,
            "GET",
            f"/{s.token}/websockify",
            {**upgrade, "Origin": f"https://{HOST}"},
        )
        head = await reader.readuntil(b"\r\n\r\n")
        assert b"101 Switching Protocols" in head
        assert b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" in head
        assert b"Sec-WebSocket-Protocol: binary" in head
        assert (await reader.readexactly(2 + 12))[2:] == b"RFB 003.008\n"
        assert s.viewers == 1
        mask = b"\x01\x02\x03\x04"
        payload = b"RFB 003.008\n"
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        writer.write(bytes([0x82, 0x80 | len(payload)]) + mask + masked)
        await writer.drain()
        echo = await reader.readexactly(2 + len(payload))
        assert echo == websocket.frame(websocket.BINARY, payload) and got == [payload]
        writer.write(
            bytes([0x88, 0x82])
            + mask
            + bytes(b ^ mask[i] for i, b in enumerate(b"\x03\xe8"))
        )
        await writer.drain()
        await reader.read()
        await asyncio.sleep(0.05)
        assert s.viewers == 0
        server.close()

    test(tmp_path)


async def post(port, path, body=None, origin=f"https://{HOST}"):
    data = json.dumps(body).encode() if body is not None else b""
    headers = {
        "Origin": origin,
        "Content-Type": "application/json",
        "Content-Length": str(len(data)),
    }
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = [
        f"POST {path} HTTP/1.1",
        f"Host: {HOST}",
        *(f"{k}: {v}" for k, v in headers.items()),
    ]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + data)
    await writer.drain()
    head, _, rest = (await reader.read()).partition(b"\r\n\r\n")
    return head.decode(), rest


def test_the_view_saves_and_deletes_logins_but_never_shows_a_secret(tmp_path):
    @with_view
    async def test(r, port, podman, tmp_path):
        s = r.sessions["career"]
        head, body = await post(port, f"/{s.token}/logins", {
            "site": "https://www.linkedin.com/", "username": "alice", "password": "hunter2",
            "totp": "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", "ask": True,
        })  # fmt: skip
        assert "200 OK" in head and b"hunter2" not in body and b"GEZDGNBV" not in body
        [login] = json.loads(body)["logins"]
        assert login["site"] == "linkedin.com" and login["totp"] and login["ask"]
        head, body = await post(
            port, f"/{s.token}/logins", {"site": "localhost", "password": "x"}
        )
        assert "400" in head and "isn't a site's name" in json.loads(body)["error"]
        head, _ = await post(
            port,
            f"/{s.token}/logins",
            {"site": "x.com", "password": "x"},
            origin="https://evil.example",
        )
        assert "403" in head
        await post(port, f"/{s.token}/logins/{login['id']}/ask", {"ask": False})
        assert not r.vault.logins("career")[0]["ask"]
        state = (await answer(port, "GET", f"/{s.token}/state"))[1]
        assert b"hunter2" not in state and b"GEZDGNBV" not in state
        await post(port, f"/{s.token}/logins/{login['id']}/delete")
        assert r.vault.logins("career") == []
        assert "404" in (await post(port, f"/{s.token}/logins/x/frobnicate"))[0]
        head, _ = await post(port, f"/{s.token}/logins", None)  # no body: no site
        assert "400" in head

    test(tmp_path)


def test_the_view_answers_the_agents_request_and_saves_offers(tmp_path):
    @with_view
    async def test(r, port, podman, tmp_path):
        s = r.sessions["career"]
        saved = r.vault.add("career", "example.com", "alice", "pw", ask=True)
        waiting = await r.op_login(scope(), saved["id"], "e1", "e2")
        state = json.loads((await answer(port, "GET", f"/{s.token}/state"))[1])
        assert state["approvals"] == [
            {
                "id": waiting["approval"],
                "kind": "login",
                "site": "example.com",
                "username": "alice",
                "url": "https://example.com/",
                "chat": "Title of https://example.com/",  # its tab's page
            }
        ]
        head, body = await post(port, f"/{s.token}/approve/{waiting['approval']}")
        assert "200 OK" in head and json.loads(body)["approvals"] == []
        assert (await r.op_login(scope(), saved["id"], "e1", "e2"))["page"]
        driver = podman.drivers[s.name]
        await r.take(s)  # logins are sent, and offered, while the user has it
        driver.offers["0a1b2c3d"] = {
            "site": "example.com",
            "username": "bob",
            "password": "typed",
        }
        state = json.loads((await answer(port, "GET", f"/{s.token}/state"))[1])
        assert state["offers"] == [
            {"id": "0a1b2c3d", "site": "example.com", "username": "bob"}
        ]
        head, body = await post(
            port, f"/{s.token}/offers/0a1b2c3d/save", {"username": "bob@example.com"}
        )
        assert b"typed" not in body and json.loads(body)["offers"] == []
        assert (
            r.vault.get("career", r.vault.logins("career")[1]["id"])["password"]
            == "typed"
        )
        head, body = await post(port, f"/{s.token}/offers/0a1b2c3d/save", {})
        assert "400" in head and "isn't waiting" in json.loads(body)["error"]

    test(tmp_path)


def test_the_views_poll_asks_the_driver_for_offers_only_while_there_can_be_some(
    tmp_path,
):
    @with_view
    async def test(r, port, podman, tmp_path):
        s = r.sessions["career"]
        driver = podman.drivers[s.name]

        async def poll():
            before = driver.calls.count(("offers", {}))
            state = json.loads((await answer(port, "GET", f"/{s.token}/state"))[1])
            return state["offers"], driver.calls.count(("offers", {})) - before

        # The agent has it and nothing was ever captured: nothing to ask.
        assert await poll() == ([], 0)
        assert await poll() == ([], 0)
        await r.op_handoff(scope(), "log in")
        assert await poll() == ([], 1)
        driver.offers["0a1b2c3d"] = {
            "site": "example.com",
            "username": "bob",
            "password": "typed",
        }
        await r.op_handoff(scope(), done=True)
        # Back with the agent, what the user sent is still offered until it's gone.
        offer = {"id": "0a1b2c3d", "site": "example.com", "username": "bob"}
        assert await poll() == ([offer], 1)
        driver.offers.clear()  # dropped, or ran out
        assert await poll() == ([], 1)
        assert await poll() == ([], 0)
        await r.take(s)
        assert await poll() == ([], 1)

    test(tmp_path)


def test_the_user_makes_a_passkey_in_the_view_and_never_sees_its_key(tmp_path):
    @with_view
    async def test(r, port, podman, tmp_path):
        s = r.sessions["career"]
        driver = podman.drivers[s.name]
        head, body = await post(port, f"/{s.token}/passkeys/make", {"on": True})
        assert "400" in head and "take over" in json.loads(body)["error"]
        await post(port, f"/{s.token}/take")
        head, _ = await post(
            port,
            f"/{s.token}/passkeys/make",
            {"on": True},
            origin="https://evil.example",
        )
        assert "403" in head and not driver.making
        head, body = await post(port, f"/{s.token}/passkeys/make", {"on": True})
        assert "200 OK" in head and json.loads(body)["making"] and driver.making
        driver.made.append({"credential": passkey("example.com"), "url": ""})
        head, body = await post(port, f"/{s.token}/passkeys/make", {"on": False})
        state = json.loads(body)
        assert not state["making"] and not driver.making
        assert state["made"] == "Saved the passkey you made for example.com as alice"
        [shown] = state["logins"]
        assert shown["kind"] == "passkey" and shown["ask"]
        assert passkey()["privateKey"].encode() not in body and b"AQID" not in body
        await post(port, f"/{s.token}/logins/{shown['id']}/delete")
        assert r.vault.logins("career") == []

    test(tmp_path)


def test_the_form_for_a_login_the_agent_asked_for(tmp_path):
    @with_view
    async def test(r, port, podman, tmp_path):
        s = r.sessions["career"]
        await r.op_open(scope(), "https://accounts.example.com/signin?next=<b>")
        asked = await r.op_ask_login(scope())
        base = f"/login/{asked['request']}"
        head, _ = await answer(port, "GET", base)
        assert "302" in head and f"Location: {base}/" in head
        head, body = await answer(port, "GET", f"{base}/")
        page = body.decode()
        assert "200 OK" in head and f"Content-Security-Policy: {takeover.CSP}" in head
        assert (
            "Referrer-Policy: no-referrer" in head and "Cache-Control: no-store" in head
        )
        assert "<title>Log in to example.com</title>" in page
        assert "a site of <strong>example.com</strong>" in page
        assert 'class="warning">' in page  # no login for example.com yet
        assert (
            "next=&lt;b&gt;" in page and "<b>" not in page
        )  # the page's address, as text
        assert '<option value="accounts.example.com">' in page
        assert '<option value="example.com">' in page
        assert 'id="ask-form">' in page and 'id="result" hidden' in page
        for f in ("login.js", "style.css"):
            assert "200 OK" in (await answer(port, "GET", f"{base}/{f}"))[0]
        # The take-over view lists it, with no more than its site.
        state = json.loads((await answer(port, "GET", f"/{s.token}/state"))[1])
        assert state["asked"] == [
            {"id": asked["request"], "site": "accounts.example.com", "link": f"{base}/"}
        ]
        # Only from the page's own origin, and only for its sites.
        save = {"site": "example.com", "username": "alice", "password": "hunter2"}
        head, _ = await post(port, f"{base}/save", save, origin="https://evil.example")
        assert "403" in head and r.vault.logins("career") == []
        head, body = await post(port, f"{base}/save", {**save, "site": "evil.example"})
        assert "400" in head and "not 'evil.example'" in json.loads(body)["error"]
        head, body = await post(port, f"{base}/save", save)
        assert "200 OK" in head and b"hunter2" not in body
        assert json.loads(body)["state"] == "saved"
        [login] = r.vault.logins("career")
        assert login["site"] == "example.com" and login["username"] == "alice"
        assert r.vault.get("career", login["id"])["password"] == "hunter2"
        head, body = await post(port, f"{base}/save", {**save, "password": "other"})
        assert "400" in head and "isn't waiting" in json.loads(body)["error"]
        head, body = await answer(port, "GET", f"{base}/")
        assert 'id="ask-form" hidden' in body.decode() and b"Saved." in body
        state = json.loads((await answer(port, "GET", f"/{s.token}/state"))[1])
        assert state["asked"] == []
        assert (
            json.loads((await answer(port, "GET", f"{base}/state"))[1])["state"]
            == "saved"
        )
        assert "404" in (await post(port, f"{base}/frobnicate"))[0]
        # Another request, turned down; and one nobody made.
        await r.op_open(scope(thread="8"), "https://example.org/")
        other = (await r.op_ask_login(scope(thread="8")))["request"]
        head, body = await post(port, f"/login/{other}/drop")
        assert json.loads(body)["state"] == "declined"
        assert "404" in (await answer(port, "GET", "/login/lr-" + "0" * 32 + "/"))[0]
        assert "404" in (await answer(port, "GET", f"/login/{s.token}/"))[0]

    test(tmp_path)


def test_the_field_types_into_the_browser_only_while_its_yours_and_says_it_nowhere(
    tmp_path, caplog
):
    @with_view
    async def test(r, port, podman, tmp_path):
        s = r.sessions["career"]
        tab = r.threads[("career", "7")]
        driver = podman.drivers[s.name]
        secret = "correct-horse-battery-staple"
        # The agent has it: refused, as VNC's keys are.
        head, body = await post(port, f"/{s.token}/type", {"text": secret})
        assert (
            "400" in head and "take over the browser first" in json.loads(body)["error"]
        )
        head, _ = await post(port, f"/{s.token}/key", {"key": "Enter"})
        assert "400" in head and driver.typed == []
        await post(port, f"/{s.token}/take")
        # Only from the page's own origin.
        head, _ = await post(
            port, f"/{s.token}/type", {"text": secret}, "https://evil.example"
        )
        assert "403" in head and driver.typed == []
        # Into the tab the view shows (its ?tab=), the chat's thread's page.
        head, body = await post(
            port, f"/{s.token}/type", {"text": "alice@example.com", "tab": tab.id}
        )
        assert "200 OK" in head and json.loads(body)["control"] == "user"
        await post(port, f"/{s.token}/key", {"key": "Tab", "tab": tab.id})
        head, body = await post(
            port, f"/{s.token}/type", {"text": secret, "secret": True, "tab": tab.id}
        )
        assert "200 OK" in head
        # Without one, or with one not open in this browser: the page last brought forward.
        await post(port, f"/{s.token}/key", {"key": "Enter", "tab": "bw-0000"})
        await post(port, f"/{s.token}/type", {"text": "x"})
        assert driver.typed == [
            ("alice@example.com", False, "7"), ("Tab", "key", "7"), (secret, True, "7"),
            ("Enter", "key", ""), ("x", False, ""),
        ]  # fmt: skip
        for bad in ({"key": "Control+v"}, {"key": "Escape"}, {"key": 1}, {}):
            head, body = await post(port, f"/{s.token}/key", bad)
            assert "400" in head and "the key is one of" in json.loads(body)["error"], (
                bad
            )
        for bad in ({"text": ""}, {"text": 7}, {}):
            head, _ = await post(port, f"/{s.token}/type", bad)
            assert "400" in head, bad
        head, _ = await post(port, f"/{s.token}/type", {"text": "x" * 20_000})
        assert "400" in head  # over the view's body limit
        assert len(driver.typed) == 5
        state = (await answer(port, "GET", f"/{s.token}/state"))[1]
        await post(port, f"/{s.token}/give")
        assert secret.encode() not in state and "alice@" not in tab.last
        assert secret not in tab.last and secret not in json.dumps(vars(s), default=str)
        assert not any(secret in rec.getMessage() for rec in caplog.records)
        assert not any(
            "alice@example.com" in rec.getMessage() for rec in caplog.records
        )

    caplog.set_level("DEBUG")
    test(tmp_path)
