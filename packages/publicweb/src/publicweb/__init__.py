"""An httpx client that refuses hosts on the LAN, the tailnet, loopback and the like, and a
download capped in size and time to use it with.

The servers that use it fetch whatever URLs the agent or the web handed them, and they
run next to AnythingLLM's API and the router, so a URL must not be able to reach those.
`public_address` is that rule; the egress proxy (packages/egress) applies the same one.

Config (environment):
  EGRESS_PROXY  the egress proxy (http://<ip>:<port>) a service container goes out
                through. Set, public_client sends everything through it and checks only
                the scheme: the proxy checks the address it connects to. Unset (on the
                host), the client checks and connects itself.
"""

import ipaddress
import os
import socket
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpcore
import httpx


def host_name(url: str) -> str:
    """A URL's host without www., for naming a source (the URL itself if it has none)."""
    return (urlsplit(url).hostname or url).removeprefix("www.")


def public_address(
    host: str, port: int, error: type[Exception] = PermissionError
) -> str:
    """An address `host` resolves to, or `error` unless all of them are public. Connect to
    the address it returns, not to `host`: a second lookup may answer something else."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise error(f"can't resolve {host}: {e}") from None
    except (
        ValueError
    ) as e:  # UnicodeError too: an empty or over-long label, e.g. cdn..example
        raise error(f"{host} isn't a valid host name: {e}") from None
    if not all(ipaddress.ip_address(info[4][0]).is_global for info in infos):
        raise error(
            f"{host} is a private or local address; only public hosts are allowed."
        )
    return str(infos[0][4][0])  # an IPv4 or IPv6 address


def check_scheme(
    request: httpx.Request, error: type[Exception] = PermissionError
) -> None:
    """Raise `error` unless the request goes over http(s)."""
    if request.url.scheme not in ("http", "https"):
        raise error(f"only http and https URLs are allowed: {request.url}")


def check_public(
    request: httpx.Request, error: type[Exception] = PermissionError
) -> None:
    """Raise `error` unless the request goes over http(s) to a host with only public addresses."""
    check_scheme(request, error)
    public_address(request.url.host, request.url.port or 443, error)


class PublicBackend(httpcore.NetworkBackend):
    """Connects only to an address it has just checked is public.

    check_public's lookup and the one httpcore makes to connect are separate, so a name
    with a zero TTL could answer a public address to the first and 127.0.0.1 to the second
    (DNS rebinding). This resolves once and connects to that address. TLS still verifies
    the request's host name: httpcore takes it from the request, not from this connection.
    """

    def __init__(self, error: type[Exception]):
        self.error = error
        self.inner: httpcore.NetworkBackend = httpcore.SyncBackend()

    def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        address = public_address(host, port, self.error)
        return self.inner.connect_tcp(
            address, port, timeout, local_address, socket_options
        )


def public_client(
    error: type[Exception], transport: httpx.BaseTransport | None = None, **kwargs
) -> httpx.Client:
    """An httpx.Client that follows redirects and checks every request, redirects included,
    and connects only to the address it checked.

    Proxy settings from the environment are ignored: through a proxy, the address checked
    here wouldn't be the one connected to. The exception is EGRESS_PROXY, the egress
    proxy, which makes the same check itself and connects to the address it checked; with
    it set, every request goes through it and only the scheme is checked here.
    """
    proxy = os.environ.get("EGRESS_PROXY")
    if proxy:
        check = check_scheme
        if transport is None:  # tests pass a MockTransport
            transport = httpx.HTTPTransport(proxy=proxy)
    else:
        check = check_public
        if transport is None:
            transport = httpx.HTTPTransport()
            # httpx has no setting for the network backend, so swap it into the pool it built
            # (pinned to httpx 0.28 / httpcore 1 in pyproject; test_connects_only_to_the_checked_address guards it).
            pool = transport._pool  # no proxy, so a plain pool
            assert isinstance(pool, httpcore.ConnectionPool)
            pool._network_backend = PublicBackend(error)
    return httpx.Client(
        transport=transport,
        follow_redirects=True,
        trust_env=False,
        event_hooks={"request": [lambda request: check(request, error)]},
        **kwargs,
    )


def _size(n: int) -> str:
    return f"{n // 2**30} GB" if n >= 2**30 else f"{n // 2**20} MB"


@contextmanager
def stream(
    client: httpx.Client,
    url: str,
    max_bytes: int,
    error: type[Exception],
    deadline: float | None = None,
    too_slow: str = "too slow",
    **kwargs,
) -> Iterator[tuple[httpx.Response, Iterator[bytes]]]:
    """GET `url`: the response, its status checked, and its body as it arrives, which raises
    `error` once it passes `max_bytes` or `deadline` (a time.monotonic() value).

    `deadline` bounds the whole read, so a server that trickles bytes can't hold a caller
    past it. HTTP errors are httpx's own; `kwargs` go to `client.stream`.
    """
    if deadline is not None:
        left = deadline - time.monotonic()
        if left <= 0:
            raise error(too_slow)
        own = client.timeout
        kwargs.setdefault(
            "timeout",
            httpx.Timeout(
                min(own.read or left, left), connect=min(own.connect or left, left)
            ),
        )
    too_large = f"larger than {_size(max_bytes)}"
    with client.stream("GET", url, **kwargs) as resp:
        resp.raise_for_status()
        length = resp.headers.get("content-length", "")
        if length.isdigit() and int(length) > max_bytes:
            raise error(too_large)

        def body() -> Iterator[bytes]:
            size = 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise error(too_large)
                if deadline is not None and time.monotonic() > deadline:
                    raise error(too_slow)
                yield chunk

        yield resp, body()


def read(
    client: httpx.Client,
    url: str,
    max_bytes: int,
    error: type[Exception],
    deadline: float | None = None,
    too_slow: str = "too slow",
    **kwargs,
) -> tuple[bytes, httpx.Response]:
    """The body (see `stream`), and the response for its headers and redirects."""
    with stream(client, url, max_bytes, error, deadline, too_slow, **kwargs) as (
        resp,
        body,
    ):
        return b"".join(body), resp


def save(body: Iterable[bytes], file: Path) -> int:
    """Write `body` to `file` by way of `file`.part, so `file` is whole or absent; returns
    its size. The .part goes if anything fails."""
    part = file.with_name(file.name + ".part")
    size = 0
    try:
        with open(part, "wb") as out:
            for chunk in body:
                out.write(chunk)
                size += len(chunk)
        os.replace(part, file)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return size
