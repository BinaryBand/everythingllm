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


# What a service container's PodmanArgs may say: the limits Quadlet 5.4 has no key for.
PODMAN_ARGS = re.compile(r"--(memory|cpus|umask)=\S+")
# Keys that would widen what a container may do or see, whatever its Volume= lines say.
WIDENING = (
    "AddDevice",
    "Mount",
    "Rootfs",
    "SecurityLabelDisable",
    "SeccompProfile",
    "Unmask",
    "Mask",
    "Sysctl",
    "Volatile",
)
# Folders no service container mounts whole: home, the data dir and storage (each takes
# its own folders of them), and the user's runtime folder (podman's and systemd's sockets).
TOO_WIDE = {
    "/",
    "%h",
    "%h/.local",
    "%h/.local/share",
    "%h/.local/share/everythingllm",
    "%h/.local/share/everythingllm/venvs",
    "@ANYTHINGLLM_STORAGE@",
    "@ANYTHINGLLM_STORAGE@/everythingllm",
    "%t",
}


@pytest.mark.parametrize("template", services(), ids=lambda t: t.name)
def test_a_service_container_gets_nothing_beyond_its_mounts_and_limits(template):
    name = template.name.removesuffix(".container.in")
    keys = container_keys(template)
    for args in keys["PodmanArgs"]:
        for arg in args.split():
            assert PODMAN_ARGS.fullmatch(arg), (name, arg)
    for key in WIDENING:
        assert key not in keys, (name, key)
    for volume in keys["Volume"]:
        source = volume.split(":")[0].rstrip("/")
        assert source not in TOO_WIDE, (name, volume)
        assert "podman" not in source and not source.endswith(".sock"), (name, volume)
        # Its own folder of venvs/, not another container's or a host service's.
        if "/venvs/" in source:
            assert source.endswith(f"/venvs/{name}-ctr"), (name, volume)


def test_sites_runner_mounts_only_what_it_uses():
    """sites-runner (sites.tools, sites.build, sites.articles_web) reads the repo, writes
    the entries and builds into the pages site, serves its socket, asks the sandbox runner
    to build, and reads AnythingLLM's .env for the article writer's key. Nothing else."""
    keys = container_keys(QUADLET / "sites-runner.container.in")
    data, storage = "%h/.local/share/everythingllm", "@ANYTHINGLLM_STORAGE@"
    assert sorted(keys["Volume"]) == sorted(
        [
            "@REPO@:@REPO@:ro",
            f"{data}/pages/entries:{data}/pages/entries",
            f"{data}/pages/public:{data}/pages/public",
            f"{storage}/everythingllm/sites:{storage}/everythingllm/sites",
            f"{storage}/everythingllm/sandbox:{storage}/everythingllm/sandbox:ro",
            f"{storage}/.env:{storage}/.env:ro",
            f"{data}/venvs/sites-runner-ctr:{data}/venvs/sites-runner-ctr",
        ]
    )
    # Its socket is in storage, so it keeps the host user's groups.
    assert keys["GroupAdd"] == ["keep-groups"]
    env = dict(e.partition("=")[::2] for e in keys["Environment"])
    # The article writer listens where the published port arrives, and reaches SearXNG by
    # the tailnet name the sites profile allows; there's no zola here, so only the sandbox
    # builds.
    assert keys["PublishPort"] == ["127.0.0.1:8448:8448"]
    assert env["ARTICLES_HOST"] == "0.0.0.0"
    assert env["SEARXNG_URL"] == "https://@PUBLIC_HOST@:8888/search"
    assert env["SITES_SANDBOX_ONLY"] == "1"
    assert env["UV_PROJECT_ENVIRONMENT"] == f"{data}/venvs/sites-runner-ctr/venv"
    assert env["UV_CACHE_DIR"] == f"{data}/venvs/sites-runner-ctr/uv-cache"
    assert keys["Exec"] == [
        "uv run --frozen --no-dev --project @REPO@ --package sites --extra host sites-runner"
    ]


def test_the_sites_profile_lets_through_what_sites_runner_reaches(egress):
    """Feeds, story pages and DeepSeek are public hosts; SearXNG is on the tailnet."""
    sites = egress.profile_for(egress.ips()["sites-runner"])
    assert sites is not None and sites.name == "sites"
    assert sites.judge("api.deepseek.com", 443) == "public"
    assert sites.judge("host.example.ts.net", 8888) == "allow"
    # Not AnythingLLM, nor the pages sites: it writes those to disk.
    for port in (3001, 8445, 8447):
        assert sites.judge("host.example.ts.net", port) is None
