import socket
import time

import httpcore
import httpx
import pytest
from publicweb import (
    PublicBackend,
    check_public,
    public_address,
    public_client,
    read,
    save,
    stream,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://10.0.0.5/x",
        "http://100.100.1.1/x",
        "http://[::1]/x",
        "http://localhost:3001/api",
    ],
)
def test_private_hosts_refused(url):
    with pytest.raises(PermissionError, match="private or local"):
        check_public(httpx.Request("GET", url))


def test_only_http_allowed():
    with pytest.raises(PermissionError, match="only http"):
        check_public(httpx.Request("GET", "file:///etc/passwd"))


def test_client_raises_the_callers_error():
    class Mine(Exception):
        pass

    with public_client(Mine) as client, pytest.raises(Mine):
        client.get("http://127.0.0.1/")


@pytest.mark.parametrize("host", ["cdn..example.net", "a" * 64 + ".example"])
def test_malformed_host_raises_the_callers_error(host):
    class Mine(Exception):
        pass

    with pytest.raises(Mine, match="valid host name"):
        check_public(httpx.Request("GET", f"https://{host}/ep.mp3"), Mine)


def test_redirects_are_checked():
    class Mine(Exception):
        pass

    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    # A public IP literal, so the first request needs no DNS.
    with (
        public_client(Mine, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(Mine, match="private or local"),
    ):
        client.get("http://93.184.216.34/feed.xml")
    assert seen == ["http://93.184.216.34/feed.xml"]


def test_proxy_environment_ignored(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.1:3128")
    with public_client(PermissionError) as client:
        assert not client._mounts


def test_the_egress_proxy_carries_everything_and_checks_the_address(monkeypatch):
    # In a service container, the egress proxy makes the address check (packages/egress):
    # the client sends every request to it, plain http and https alike, and only refuses
    # what isn't http(s).
    monkeypatch.setenv("EGRESS_PROXY", "http://10.89.79.2:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.1:3128")  # still ignored
    with public_client(PermissionError) as client:
        assert not client._mounts
        pool = client._transport._pool
        assert isinstance(pool, httpcore.HTTPProxy)
        assert pool._proxy_url.host == b"10.89.79.2" and pool._proxy_url.port == 3128
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200)

    with public_client(PermissionError, transport=httpx.MockTransport(handler)) as c:
        c.get("http://10.0.0.5/x")  # the proxy's to refuse, not the client's
        with pytest.raises(PermissionError, match="only http"):
            c.get("ftp://example.com/x")
    assert seen == ["http://10.0.0.5/x"]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.0.29",
        "169.254.1.2",  # link-local: host.containers.internal
        "100.89.16.22",  # CGNAT (Tailscale's)
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:10.0.0.1",
    ],
)
def test_public_address_refuses_whatever_isnt_public(monkeypatch, address):
    fake_dns(monkeypatch, address)
    with pytest.raises(PermissionError, match="private or local"):
        public_address("anything.example", 443)


def test_public_address_wants_every_answer_public(monkeypatch):
    def getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    with pytest.raises(PermissionError, match="private or local"):
        public_address("mixed.example", 443)
    fake_dns(monkeypatch, "2606:4700::1")
    assert public_address("v6.example", 443) == "2606:4700::1"


class Inner(httpcore.NetworkBackend):
    """A network backend that records where it was asked to connect, and connects nowhere."""

    def __init__(self):
        self.hosts = []

    def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        self.hosts.append((host, port))
        raise httpcore.ConnectError("not really connecting")

    def sleep(self, seconds):
        pass


def fake_dns(monkeypatch, *answers):
    """Each lookup returns the next answer: a rebinding name, public first and local next."""
    calls = iter(answers)

    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(calls), port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def test_backend_connects_to_the_address_it_checked(monkeypatch):
    fake_dns(monkeypatch, "93.184.215.14", "127.0.0.1")
    backend = PublicBackend(PermissionError)
    backend.inner = inner = Inner()
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("rebind.example", 443)
    assert inner.hosts == [("93.184.215.14", 443)]


def test_backend_refuses_a_private_answer(monkeypatch):
    fake_dns(monkeypatch, "127.0.0.1")
    backend = PublicBackend(PermissionError)
    backend.inner = inner = Inner()
    with pytest.raises(PermissionError, match="private or local"):
        backend.connect_tcp("rebind.example", 443)
    assert inner.hosts == []


def test_connects_only_to_the_checked_address(monkeypatch):
    # The request hook's lookup sees a public address and the connect's lookup sees
    # 127.0.0.1: the client must refuse rather than connect there.
    fake_dns(monkeypatch, "93.184.215.14", "127.0.0.1")
    with public_client(PermissionError) as client:
        # httpx/httpcore internals still as expected
        transport = client._transport
        assert isinstance(transport, httpx.HTTPTransport)
        assert isinstance(transport._pool, httpcore.ConnectionPool)
        backend = transport._pool._network_backend
        assert isinstance(backend, PublicBackend)
        backend.inner = inner = Inner()
        with pytest.raises(PermissionError, match="private or local"):
            client.get("http://rebind.example/")
    assert inner.hosts == []


class Refused(Exception):
    pass


def serving(content, **headers):
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=content, headers=headers)
        )
    )


def slow(chunks, pause):
    for _ in range(chunks):
        time.sleep(pause)
        yield b"x" * 10


def test_read_returns_the_body_and_response():
    body, resp = read(
        serving(b"hello", **{"content-type": "text/plain"}),
        "https://a.example/",
        100,
        Refused,
    )
    assert body == b"hello" and resp.headers["content-type"] == "text/plain"


def test_cap_from_content_length_and_while_reading():
    with pytest.raises(Refused, match="larger than 5 MB"):
        read(
            serving(b"x", **{"content-length": str(10**9)}),
            "https://a.example/",
            5 * 2**20,
            Refused,
        )
    with pytest.raises(
        Refused, match="larger than 1 MB"
    ):  # a body with no length given
        read(
            serving(b"x" * 2**19 for _ in range(3)),
            "https://a.example/",
            2**20,
            Refused,
        )
    with pytest.raises(Refused, match="larger than 2 GB"):
        read(
            serving(b"x", **{"content-length": str(3 * 2**30)}),
            "https://a.example/",
            2 * 2**30,
            Refused,
        )


@pytest.mark.xdist_group("timing")
def test_deadline_bounds_the_whole_read():
    with pytest.raises(Refused, match="gave up"):
        read(
            serving(b"x"),
            "https://a.example/",
            100,
            Refused,
            time.monotonic() - 1,
            too_slow="gave up",
        )
    started = time.monotonic()
    with pytest.raises(Refused, match="too slow"):
        read(
            serving(slow(50, 0.02)),
            "https://a.example/",
            10**6,
            Refused,
            time.monotonic() + 0.2,
        )
    assert time.monotonic() - started < 0.6


def test_http_errors_are_httpx_own():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(httpx.HTTPStatusError):
        read(client, "https://a.example/", 100, Refused)


def test_save_leaves_a_whole_file_or_none(tmp_path):
    file = tmp_path / "a.mp3"
    with stream(serving(b"abc"), "https://a.example/", 100, Refused) as (_, body):
        assert save(body, file) == 3
    assert file.read_bytes() == b"abc"
    with (
        stream(serving(slow(5, 0)), "https://a.example/", 25, Refused) as (_, body),
        pytest.raises(Refused),
    ):
        save(body, tmp_path / "b.mp3")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.mp3"]
