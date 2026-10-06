"""Every service container's Quadlet template keeps to the shared hardening and to its
address in egress.toml (README, "Service containers"): the proxy then knows it by that
address, and it has no other way out."""

import re
from collections import defaultdict
from pathlib import Path

import pytest
from egress import config as egress_config

QUADLET = Path(__file__).resolve().parents[3] / "host" / "quadlet"
# Containers that aren't service containers: AnythingLLM and the pages site's Caddy.
NOT_SERVICES = {"anythingllm", "static_agent"}
PROXY = "egress-proxy"
HARDENING = {
    "ReadOnly": "true",
    "DropCapability": "ALL",
    "NoNewPrivileges": "true",
    "UserNS": "keep-id",
    "RunInit": "true",
    "Image": "localhost/everythingllm-service",
}


def container_keys(template: Path) -> dict[str, list[str]]:
    """The [Container] section's keys, each with every value it's given."""
    keys, section = defaultdict(list), ""
    for line in template.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line
        elif section == "[Container]":
            key, _, value = line.partition("=")
            keys[key].append(value)
    return keys


def services() -> list[Path]:
    return sorted(
        t
        for t in QUADLET.glob("*.container.in")
        if t.name.removesuffix(".container.in") not in NOT_SERVICES
    )


@pytest.fixture(scope="module")
def egress():
    return egress_config.load(env={"PUBLIC_HOST": "host.example.ts.net"})


def test_the_proxy_is_one_of_them():
    assert QUADLET / f"{PROXY}.container.in" in services()


@pytest.mark.parametrize("template", services(), ids=lambda t: t.name)
def test_a_service_container_is_hardened(template):
    keys = container_keys(template)
    for key, value in HARDENING.items():
        assert keys[key] == [value], (template.name, key)
    assert [t.split(":")[0] for t in keys["Tmpfs"]] == ["/tmp"]
    assert keys["PidsLimit"] and re.search(
        r"--memory=\S+", " ".join(keys["PodmanArgs"])
    )
    assert "--cpus=" in " ".join(keys["PodmanArgs"])
    # Unsupported by Quadlet 5.4 (PodmanArgs has them), or undoing the above.
    for key in ("Memory", "Umask", "AddCapability", "ContainerName", "Pod"):
        assert key not in keys, (template.name, key)
    # The repo read-only at its own path, so paths mean the same inside and out.
    assert "@REPO@:@REPO@:ro" in keys["Volume"]
    for volume in keys["Volume"]:
        source, target, *_ = volume.split(":")
        assert source == target, volume
    for port in keys["PublishPort"]:
        assert port.startswith("127.0.0.1:"), port


@pytest.mark.parametrize("template", services(), ids=lambda t: t.name)
def test_a_service_container_goes_out_only_through_the_proxy(template, egress):
    name = template.name.removesuffix(".container.in")
    keys = container_keys(template)
    env = dict(e.partition("=")[::2] for e in keys["Environment"])
    if name == PROXY:
        # The one way out: egress-net at the proxy's address, and podman's network.
        assert keys["Network"] == [f"{egress.network}:ip={egress.proxy}", "podman"]
        assert not {"HTTPS_PROXY", "HTTP_PROXY", "EGRESS_PROXY"} & set(env)
        return
    ips = egress.ips()
    assert name in ips, f"{name} has no address in egress.toml"
    assert keys["Network"] == [f"{egress.network}:ip={ips[name]}"]
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "EGRESS_PROXY"):
        assert env.get(key) == egress.url, (name, key)
