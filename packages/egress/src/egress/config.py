"""egress.toml (next to this file) read into the proxy's network and profiles, and the one
rule each connection is judged by (`Profile.judge`).

A profile is what one service may reach: public hosts on ports 80 and 443 (if `public`),
plus its `allow` exceptions, plus the network's `allow` (PyPI) that every profile has. The
proxy knows a connection's profile by its source address on egress-net (`ips`).

Config (environment):
  any @KEY@ in egress.toml's allow entries (PUBLIC_HOST, NTFY_HOST), else its [defaults]
"""

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import tomllib

FILE = Path(__file__).with_name("egress.toml")
PLACEHOLDER = re.compile(r"@([A-Z_]+)@")
PORTS = frozenset({80, 443})  # what `public` opens


def normal_host(host: str) -> str:
    """A host as the rules compare it: lower case, no trailing dot, no IPv6 brackets."""
    return host.strip().removeprefix("[").removesuffix("]").rstrip(".").lower()


def host_port(entry: str) -> tuple[str, int]:
    """("pypi.org", 443) from "pypi.org:443"; "[::1]:443" for an IPv6 address.
    ValueError for anything else."""
    host, sep, port = entry.rpartition(":")
    host = normal_host(host)
    if not (sep and host and port.isdigit() and 0 < int(port) < 65536):
        raise ValueError(f"not host:port: {entry!r}")
    return host, int(port)


@dataclass(frozen=True)
class Profile:
    name: str
    public: bool
    allow: frozenset[
        tuple[str, int]
    ]  # the network's and its own, placeholders filled in
    ips: Mapping[str, str]  # container -> address on the network

    def judge(self, host: str, port: int) -> str | None:
        """How (host, port) may be reached: "allow" (an exception: connect to whatever it
        resolves to), "public" (only if every address it resolves to is public), or None
        (refused)."""
        if (normal_host(host), port) in self.allow:
            return "allow"
        if self.public and port in PORTS:
            return "public"
        return None


@dataclass(frozen=True)
class Config:
    network: str  # the podman network's name
    subnet: str
    proxy: str  # the proxy's address on it
    port: int
    profiles: dict[str, Profile]

    @property
    def url(self) -> str:
        """What a container's HTTPS_PROXY, HTTP_PROXY and EGRESS_PROXY say."""
        return f"http://{self.proxy}:{self.port}"

    def ips(self) -> dict[str, str]:
        """Every container's address on the network: container -> address."""
        return {c: ip for p in self.profiles.values() for c, ip in p.ips.items()}

    def profile_for(self, address: str) -> Profile | None:
        """The profile of a connection from `address`, or None for a stranger."""
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return None
        if ip.version == 6 and ip.ipv4_mapped:  # a dual-stack socket's view of IPv4
            ip = ip.ipv4_mapped
        return next(
            (
                p
                for p in self.profiles.values()
                if any(ipaddress.ip_address(a) == ip for a in p.ips.values())
            ),
            None,
        )


def fill(entry: str, values: Mapping[str, str]) -> str:
    missing = [k for k in PLACEHOLDER.findall(entry) if not values.get(k)]
    if missing:
        raise ValueError(f"egress.toml: {', '.join(missing)} isn't set (in {entry!r})")
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], entry)


def load(path: Path = FILE, env: Mapping[str, str] | None = None) -> Config:
    """The network and profiles, with @KEY@ filled in from `env` (default os.environ) or
    [defaults]. ValueError for an unset placeholder, a bad entry or an address that's
    outside the subnet or used twice."""
    with path.open("rb") as f:
        raw = tomllib.load(f)
    values = {**raw.get("defaults", {}), **(os.environ if env is None else env)}
    net = raw["network"]
    subnet = ipaddress.ip_network(net["subnet"])
    common = [fill(e, values) for e in net.get("allow", [])]
    profiles = {}
    for name, p in raw["profiles"].items():
        unknown = set(p) - {"public", "allow", "ips"}
        if unknown:
            raise ValueError(
                f"egress.toml [profiles.{name}]: unknown {sorted(unknown)}"
            )
        allow = [*common, *(fill(e, values) for e in p.get("allow", []))]
        profiles[name] = Profile(
            name,
            bool(p.get("public", False)),
            frozenset(host_port(e) for e in allow),
            dict(p.get("ips", {})),
        )
    config = Config(net["name"], str(subnet), net["proxy"], int(net["port"]), profiles)
    taken = [config.proxy, *config.ips().values()]
    if len(set(taken)) != len(taken):
        raise ValueError("egress.toml: an address is used twice")
    for address in taken:
        if ipaddress.ip_address(address) not in subnet:
            raise ValueError(f"egress.toml: {address} isn't in {subnet}")
    return config
