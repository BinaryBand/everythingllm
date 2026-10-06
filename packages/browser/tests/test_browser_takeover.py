"""The take-over view (browser.takeover): its page and files, taking the browser and
handing it back from the page's own origin only, and the WebSocket carried to x11vnc."""

import asyncio
import json
import os

from browser import takeover, websocket
from browser.runner import Runner
from browser_fakes import Clock, FakePodman, config, scope

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
                await r.op_open(scope(), "example.com")
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
