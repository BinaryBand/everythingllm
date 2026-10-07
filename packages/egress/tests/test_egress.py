"""The egress proxy: its profiles, and what it lets through. The proxy runs for real on
127.0.0.1 with DNS faked, and connects to a server of the test's instead of the address it
checked, which the test records."""

import asyncio
import logging
import socket

import pytest
from egress import config as egress_config
from egress import proxy
from egress.config import Config, Profile

HOST = "host.example.ts.net"
HOST_IP = "100.89.16.22"  # CGNAT, as Tailscale's addresses are
PUBLIC = "93.184.215.14"


def loaded(**env) -> Config:
    return egress_config.load(env={"PUBLIC_HOST": HOST, **env})


def test_the_profiles_fill_in_the_hosts_and_keep_to_their_addresses():
    config = loaded()
    assert (config.network, config.subnet, config.url, config.public_url) == (
        "egress-net",
        "10.89.79.0/24",
        "http://10.89.79.2:3128",
        "http://10.89.79.2:3129",
    )
    assert config.ips() == {
        "relay": "10.89.79.10",
        "research-runner": "10.89.79.11",
        "sites-runner": "10.89.79.12",
        "browser-1": "10.89.79.32",
        "browser-2": "10.89.79.33",
        "browser-3": "10.89.79.34",
        "browser-4": "10.89.79.35",
    }
    browser = config.profiles["browser"]  # public hosts, and only on the public port
    assert browser.public and browser.judge(HOST, 3001) is None
    relay, research = config.profiles["relay"], config.profiles["research"]
    assert not relay.public and research.public
    assert {(HOST, 3001), ("ntfy.sh", 443)} <= relay.allow
    assert (HOST, 8888) in research.allow
    assert (HOST, 3001) not in research.allow
    assert (HOST, 8888) in config.profiles["sites"].allow
    assert (HOST, 3001) not in config.profiles["sites"].allow
    for p in config.profiles.values():  # uv's first sync, for every container
        assert {("pypi.org", 443), ("files.pythonhosted.org", 443)} <= p.allow
    assert ("ntfy.example", 443) in loaded(NTFY_HOST="ntfy.example").profiles[
        "relay"
    ].allow


def test_a_profile_without_its_host_doesnt_load(tmp_path):
    with pytest.raises(ValueError, match="PUBLIC_HOST isn't set"):
        egress_config.load(env={})
    bad = tmp_path / "egress.toml"
    text = egress_config.FILE.read_text()
    bad.write_text(text.replace('"10.89.79.12"', '"10.89.79.11"'))
    with pytest.raises(ValueError, match="used twice"):
        egress_config.load(bad, env={"PUBLIC_HOST": HOST})
    bad.write_text(text.replace('"10.89.79.12"', '"10.89.80.12"'))
    with pytest.raises(ValueError, match="isn't in 10.89.79.0/24"):
        egress_config.load(bad, env={"PUBLIC_HOST": HOST})
    bad.write_text(text.replace('"10.89.79.12"', '"10.89.79.200"'))
    with pytest.raises(ValueError, match="which podman hands out"):
        egress_config.load(bad, env={"PUBLIC_HOST": HOST})
    bad.write_text(text.replace("public_port = 3129", "public_port = 3128"))
    with pytest.raises(ValueError, match="port and public_port are the same"):
        egress_config.load(bad, env={"PUBLIC_HOST": HOST})


def test_judging_a_host_and_port():
    config = loaded()
    research, relay = config.profiles["research"], config.profiles["relay"]
    assert research.judge("example.com", 443) == "public"
    assert research.judge("example.com", 80) == "public"
    assert research.judge("example.com", 22) is None
    assert research.judge("example.com", 8080) is None
    assert research.judge(f"{HOST.upper()}.", 8888) == "allow"
    assert research.judge(HOST, 8447) is None  # not an exception, and not public
    assert relay.judge("example.com", 443) is None  # relay has no public access
    assert relay.judge(HOST, 3001) == "allow"
    assert relay.judge("pypi.org", 443) == "allow"
    # On the public port no exception counts, the network's included.
    assert research.judge(HOST, 8888, public_only=True) is None
    assert research.judge(HOST, 3001, public_only=True) is None
    assert research.judge("pypi.org", 443, public_only=True) == "public"
    assert research.judge("example.com", 443, public_only=True) == "public"
    assert relay.judge(HOST, 3001, public_only=True) is None


def test_a_connection_is_known_by_its_address():
    config = loaded()
    assert config.profile_for("10.89.79.11").name == "research"
    assert config.profile_for("::ffff:10.89.79.10").name == "relay"
    assert config.profile_for("10.89.79.12").name == "sites"
    assert config.profile_for("10.89.79.2") is None  # the proxy itself
    assert config.profile_for("10.88.0.5") is None
    assert config.profile_for("not an address") is None


def test_a_plain_request_is_sent_on_in_origin_form_without_its_proxy_headers():
    r = proxy.parse(
        b"POST http://Example.COM./feed?x=1 HTTP/1.1\r\nHost: elsewhere\r\n"
        b"Proxy-Connection: keep-alive\r\nProxy-Authorization: Basic eA==\r\n"
        b"Connection: keep-alive, X-Secret\r\nX-Secret: 1\r\nContent-Length: 2\r\n\r\n"
    )
    assert (r.method, r.host, r.port, r.where) == (
        "POST",
        "example.com",
        80,
        "example.com:80",
    )
    assert r.forward == (
        b"POST /feed?x=1 HTTP/1.1\r\nHost: example.com\r\nContent-Length: 2\r\n"
        b"Connection: close\r\n\r\n"
    )
    r = proxy.parse(b"GET http://[2606:4700::1]:443 HTTP/1.1\r\n\r\n")
    assert (r.host, r.port, r.where) == ("2606:4700::1", 443, "[2606:4700::1]:443")
    assert r.forward.startswith(b"GET / HTTP/1.1\r\nHost: [2606:4700::1]:443\r\n")
    r = proxy.parse(b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\n")
    assert (r.method, r.host, r.port, r.forward) == ("CONNECT", "pypi.org", 443, b"")


@pytest.mark.parametrize(
    "head",
    [
        b"GET /relative HTTP/1.1\r\n\r\n",  # not a proxy request
        b"GET https://example.com/ HTTP/1.1\r\n\r\n",  # https goes by CONNECT
        b"GET ftp://example.com/ HTTP/1.1\r\n\r\n",
        b"CONNECT example.com HTTP/1.1\r\n\r\n",
        b"CONNECT example.com:http HTTP/1.1\r\n\r\n",
        b"GET http://example.com/ HTTP/2\r\n\r\n",
        b"GET http://example.com/ HTTP/1.1\r\nno colon\r\n\r\n",
        b"GET http://example.com:99999/ HTTP/1.1\r\n\r\n",
    ],
)
def test_what_isnt_a_proxy_request_is_bad(head):
    with pytest.raises(proxy.BadRequest):
        proxy.parse(head)


# --- the proxy, running ---------------------------------------------------------------


def fake_dns(monkeypatch, *answers: str | list[str]) -> list[str]:
    """Each lookup gets the next answer (one address or several); returns the names
    looked up."""
    calls = iter(answers)
    asked = []

    def getaddrinfo(host, port, *args, **kwargs):
        asked.append(host)
        answer = next(calls)
        family = {4: socket.AF_INET, 6: socket.AF_INET6}
        return [
            (family[6 if ":" in a else 4], socket.SOCK_STREAM, 6, "", (a, port))
            for a in ([answer] if isinstance(answer, str) else answer)
        ]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return asked


def config_for(client: str = "127.0.0.1") -> Config:
    """research's profile, for connections from `client`."""
    research = loaded().profiles["research"]
    profile = Profile("research", research.public, research.allow, {"t": client})
    return Config("egress-net", "127.0.0.0/8", "127.0.0.1", 0, {"research": profile})


class Upstream:
    """A server standing in for every address the proxy connects to: it records what it
    was sent and answers `answer`, then closes."""

    def __init__(
        self, answer: bytes = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
    ):
        self.answer = answer
        self.received = b""
        self.connected: list[tuple[str, int]] = []  # where the proxy meant to connect

    async def handle(self, reader, writer):
        try:  # a tunnel the client sends nothing through still gets its answer
            self.received = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 0.5)
        except (TimeoutError, asyncio.IncompleteReadError):
            pass
        writer.write(self.answer)
        await writer.drain()
        writer.close()

    async def opener(self, address: str, port: int):
        self.connected.append((address, port))
        return await asyncio.open_connection("127.0.0.1", self.port)

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]


async def through(
    config: Config, upstream: Upstream, *sends: bytes, public_only: bool = False
) -> bytes:
    """Send `sends` through the proxy, one after another, and read until it closes."""
    await upstream.start()
    p = proxy.Proxy(config, upstream.opener, public_only)
    server = await asyncio.start_server(
        p.handle, "127.0.0.1", 0, limit=proxy.HEAD_LIMIT
    )
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    for data in sends:
        writer.write(data)
        await writer.drain()
    answer = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    server.close()
    upstream.server.close()
    return answer


def run(*args, **kwargs) -> bytes:
    return asyncio.run(through(*args, **kwargs))


def test_plain_http_is_forwarded_to_the_address_checked(monkeypatch, caplog):
    caplog.set_level(logging.INFO, "egress")
    asked = fake_dns(monkeypatch, PUBLIC)
    up = Upstream()
    answer = run(
        config_for(),
        up,
        b"GET http://example.com/secret/path?token=abc HTTP/1.1\r\nHost: example.com\r\n"
        b"Proxy-Connection: keep-alive\r\n\r\n",
    )
    assert answer.endswith(b"\r\n\r\nok")
    assert up.connected == [(PUBLIC, 80)] and asked == ["example.com"]
    assert up.received == (
        b"GET /secret/path?token=abc HTTP/1.1\r\nHost: example.com\r\nConnection: close\r\n\r\n"
    )
    assert "research GET example.com:80 allowed (public)" in caplog.text
    assert "secret" not in caplog.text and "token" not in caplog.text


def test_connect_tunnels_both_ways(monkeypatch):
    fake_dns(monkeypatch, PUBLIC)
    up = Upstream(answer=b"server hello")
    answer = run(
        config_for(),
        up,
        b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n",
        b"client hello\r\n\r\n",
    )
    assert answer == b"HTTP/1.1 200 Connection established\r\n\r\nserver hello"
    assert up.connected == [(PUBLIC, 443)] and up.received == b"client hello\r\n\r\n"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "192.168.0.29",
        "172.17.0.1",
        "169.254.1.2",  # link-local: host.containers.internal
        HOST_IP,  # CGNAT (Tailscale's)
        "10.89.79.11",  # another container on egress-net
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fd00::1",
        "::ffff:127.0.0.1",  # IPv4-mapped
        "::ffff:192.168.0.1",
    ],
)
def test_whatever_isnt_public_is_refused(monkeypatch, caplog, address):
    caplog.set_level(logging.INFO, "egress")
    fake_dns(monkeypatch, address)
    up = Upstream()
    answer = run(config_for(), up, b"CONNECT sneaky.example:443 HTTP/1.1\r\n\r\n")
    assert (
        answer.startswith(b"HTTP/1.1 403 Forbidden") and b"private or local" in answer
    )
    assert up.connected == []
    assert "research CONNECT sneaky.example:443 refused" in caplog.text


def test_an_address_literal_is_judged_the_same(monkeypatch):
    # No fake DNS: getaddrinfo answers a literal itself.
    for target in (b"127.0.0.1:443", b"[::1]:443", b"[::ffff:7f00:1]:443"):
        up = Upstream()
        answer = run(config_for(), up, b"CONNECT " + target + b" HTTP/1.1\r\n\r\n")
        assert answer.startswith(b"HTTP/1.1 403"), target
        assert up.connected == []


def test_a_name_with_one_private_answer_among_public_ones_is_refused(monkeypatch):
    fake_dns(monkeypatch, [PUBLIC, "10.0.0.1"])
    up = Upstream()
    answer = run(config_for(), up, b"GET http://mixed.example/ HTTP/1.1\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 403") and up.connected == []


def test_rebinding_cant_win(monkeypatch):
    # A name that answers public first and loopback after: it's looked up once, and the
    # proxy connects to the address it checked, never to a second answer.
    asked = fake_dns(monkeypatch, PUBLIC, "127.0.0.1", "127.0.0.1")
    up = Upstream()
    run(config_for(), up, b"CONNECT rebind.example:443 HTTP/1.1\r\n\r\n", b"x\r\n\r\n")
    assert asked == ["rebind.example"]
    assert up.connected == [(PUBLIC, 443)]


def test_only_ports_80_and_443_are_open(monkeypatch):
    for target in (b"example.com:22", b"example.com:8080", b"example.com:25"):
        fake_dns(monkeypatch, PUBLIC)
        up = Upstream()
        answer = run(config_for(), up, b"CONNECT " + target + b" HTTP/1.1\r\n\r\n")
        assert answer.startswith(b"HTTP/1.1 403") and b"isn't allowed" in answer
        assert up.connected == []


def test_the_profiles_exceptions_reach_the_public_host(monkeypatch):
    fake_dns(monkeypatch, HOST_IP)
    up = Upstream(answer=b"tls")
    answer = run(config_for(), up, f"CONNECT {HOST}:8888 HTTP/1.1\r\n\r\n".encode())
    assert answer.endswith(b"tls") and up.connected == [(HOST_IP, 8888)]
    # Only on the ports named: the pages site on :8447 isn't one of research's.
    fake_dns(monkeypatch, HOST_IP)
    up = Upstream()
    answer = run(config_for(), up, f"CONNECT {HOST}:8447 HTTP/1.1\r\n\r\n".encode())
    assert answer.startswith(b"HTTP/1.1 403") and up.connected == []
    # And on 443 PUBLIC_HOST is just a name with a private address.
    fake_dns(monkeypatch, HOST_IP)
    up = Upstream()
    answer = run(config_for(), up, f"CONNECT {HOST}:443 HTTP/1.1\r\n\r\n".encode())
    assert answer.startswith(b"HTTP/1.1 403") and up.connected == []


def test_the_public_port_takes_no_exceptions(monkeypatch, caplog):
    """public_client's fetches (EGRESS_PROXY) come in on the public port: a page that links
    or redirects to AnythingLLM or SearXNG on PUBLIC_HOST gets nothing, as on the host,
    even from research, whose own clients may reach both on the other port."""
    caplog.set_level(logging.INFO, "egress")
    for port in (3001, 8888):
        fake_dns(monkeypatch, HOST_IP)
        up = Upstream()
        answer = run(
            config_for(),
            up,
            f"CONNECT {HOST}:{port} HTTP/1.1\r\n\r\n".encode(),
            public_only=True,
        )
        assert answer.startswith(b"HTTP/1.1 403") and up.connected == []
    assert "on the public port" in caplog.text
    fake_dns(monkeypatch, HOST_IP)
    up = Upstream()
    answer = run(
        config_for(),
        up,
        f"GET http://{HOST}:8888/search HTTP/1.1\r\n\r\n".encode(),
        public_only=True,
    )
    assert answer.startswith(b"HTTP/1.1 403") and up.connected == []
    # A public host is as it is on the other port.
    fake_dns(monkeypatch, PUBLIC)
    up = Upstream(answer=b"tls")
    answer = run(
        config_for(), up, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n", public_only=True
    )
    assert answer.endswith(b"tls") and up.connected == [(PUBLIC, 443)]


def test_a_profile_without_public_access_gets_only_its_exceptions(monkeypatch):
    relay = loaded().profiles["relay"]
    only = Config(
        "egress-net",
        "127.0.0.0/8",
        "127.0.0.1",
        0,
        {"relay": Profile("relay", False, relay.allow, {"relay": "127.0.0.1"})},
    )
    fake_dns(monkeypatch, PUBLIC)
    up = Upstream()
    answer = run(only, up, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 403") and up.connected == []
    fake_dns(monkeypatch, "151.101.0.223")
    up = Upstream(answer=b"tls")
    answer = run(only, up, b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n")
    assert answer.endswith(b"tls") and up.connected == [("151.101.0.223", 443)]


def test_a_stranger_is_refused(monkeypatch, caplog):
    caplog.set_level(logging.INFO, "egress")
    asked = fake_dns(monkeypatch, PUBLIC)
    up = Upstream()
    answer = run(
        config_for("10.89.79.11"), up, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n"
    )
    assert answer.startswith(b"HTTP/1.1 403") and b"not a known container" in answer
    assert up.connected == [] and asked == []
    assert "127.0.0.1 CONNECT example.com:443 refused" in caplog.text


def test_a_bad_request_is_answered_and_closed():
    up = Upstream()
    answer = run(config_for(), up, b"GET /index.html HTTP/1.1\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 400") and up.connected == []
    up = Upstream()
    answer = run(config_for(), up, b"GET http://example.com/" + b"a" * proxy.HEAD_LIMIT)
    assert answer.startswith(b"HTTP/1.1 400") and up.connected == []


def test_an_unreachable_host_is_a_502(monkeypatch):
    fake_dns(monkeypatch, PUBLIC)

    async def refuse(address, port):
        raise ConnectionRefusedError("refused")

    async def go():
        p = proxy.Proxy(config_for(), refuse)
        server = await asyncio.start_server(p.handle, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", server.sockets[0].getsockname()[1]
        )
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        answer = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        server.close()
        return answer

    assert asyncio.run(go()).startswith(b"HTTP/1.1 502")


class Scripted(Upstream):
    """An upstream that plays `script(reader, writer)` instead of answering at once."""

    def __init__(self, script):
        super().__init__()
        self.handle = script


async def tunnel(script, client) -> None:
    """A CONNECT tunnel to `script`, with `client(reader, writer)` at the near end."""
    upstream = Scripted(script)
    await upstream.start()
    p = proxy.Proxy(config_for(), upstream.opener)
    server = await asyncio.start_server(p.handle, "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", server.sockets[0].getsockname()[1]
    )
    writer.write(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
    await reader.readuntil(b"\r\n\r\n")
    try:
        await asyncio.wait_for(client(reader, writer), 5)
    finally:
        writer.close()
        server.close()
        upstream.server.close()


QUIET = 0.3  # IDLE_SECONDS, for these tests
CHUNKS = 8  # one each 0.1 s: longer than QUIET


def test_a_download_keeps_the_quiet_way_up_open(monkeypatch):
    fake_dns(monkeypatch, PUBLIC)
    monkeypatch.setattr(proxy, "IDLE_SECONDS", QUIET)
    seen = {}

    async def download(reader, writer):
        for _ in range(CHUNKS):
            writer.write(b"x")
            await writer.drain()
            await asyncio.sleep(0.1)
        seen["ended"] = reader.at_eof()  # the proxy didn't half-close the way up
        seen["after"] = await asyncio.wait_for(reader.read(100), 2)
        writer.write(b"done")
        await writer.drain()
        writer.close()

    async def client(reader, writer):
        assert await reader.readexactly(CHUNKS) == b"x" * CHUNKS
        writer.write(b"next request")  # a kept-alive connection's, say
        await writer.drain()
        assert await reader.read() == b"done"

    asyncio.run(tunnel(download, client))
    assert seen == {"ended": False, "after": b"next request"}


def test_an_upload_keeps_the_quiet_way_down_open(monkeypatch):
    fake_dns(monkeypatch, PUBLIC)
    monkeypatch.setattr(proxy, "IDLE_SECONDS", QUIET)

    async def upload(reader, writer):
        await reader.readexactly(CHUNKS)
        writer.write(b"ok")
        await writer.drain()
        writer.close()

    async def client(reader, writer):
        for _ in range(CHUNKS):
            writer.write(b"x")
            await writer.drain()
            await asyncio.sleep(0.1)
        assert await reader.read() == b"ok"

    asyncio.run(tunnel(upload, client))


def test_a_tunnel_quiet_both_ways_is_closed(monkeypatch):
    fake_dns(monkeypatch, PUBLIC)
    monkeypatch.setattr(proxy, "IDLE_SECONDS", QUIET)
    seen = {}

    async def silent(reader, writer):
        seen["upstream"] = await reader.read()  # until the proxy closes it

    async def client(reader, writer):
        start = asyncio.get_running_loop().time()
        assert await reader.read() == b""
        seen["after"] = asyncio.get_running_loop().time() - start

    asyncio.run(tunnel(silent, client))
    assert seen["upstream"] == b"" and QUIET * 0.9 <= seen["after"] < 2
