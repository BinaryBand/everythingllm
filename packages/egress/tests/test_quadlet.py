"""Every service container's Quadlet template keeps to the shared hardening and to its
address in egress.toml (README, "Service containers"): the proxy then knows it by that
address, and it has no other way out."""

import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

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


# research-runner (README, "Deep research"): what it mounts is what its code writes or
# reads outside the repo, and nothing more.
DATA = "%h/.local/share/everythingllm"
STORAGE = "@ANYTHINGLLM_STORAGE@"
RESEARCH = {
    # mount target: whether it's read-only
    "@REPO@": True,
    f"{DATA}/venvs/research-runner-ctr": False,
    f"{DATA}/research": False,  # runs/: the run log and live runs' markers
    f"{DATA}/pages/entries/research": False,
    f"{DATA}/pages/entries/.build.lock": False,
    f"{DATA}/pages/public": False,  # one mount: a build's rename stays inside it
    f"{STORAGE}/everythingllm/research": False,  # its socket
    f"{STORAGE}/everythingllm/sandbox": True,  # the sandbox's, for build_system_site
    f"{STORAGE}/.env": True,
    f"{STORAGE}/anythingllm-fs/research": False,
    f"{STORAGE}/documents/deep-research": False,
}


def mounts(keys: dict[str, list[str]]) -> dict[str, bool]:
    """Each Volume='s target, and whether it's read-only."""
    out = {}
    for volume in keys["Volume"]:
        _, target, *options = volume.split(":")
        out[target] = "ro" in options
    return out


def test_research_mounts_only_what_it_uses():
    template = QUADLET / "research-runner.container.in"
    keys = container_keys(template)
    assert mounts(keys) == RESEARCH
    assert keys["GroupAdd"] == ["keep-groups"]  # it writes in storage
    assert keys["PublishPort"] == ["127.0.0.1:8450:8450"]
    # What it mounts from the host is made first: podman won't mount what isn't there.
    made = re.findall(
        r"^ExecStartPre=/usr/bin/(?:mkdir -p|touch) (.+)$",
        template.read_text(),
        re.MULTILINE,
    )
    made = {path for line in made for path in line.split()}
    assert set(RESEARCH) - {"@REPO@", f"{STORAGE}/.env"} <= made


def test_research_mounts_are_where_its_code_goes(monkeypatch, tmp_path):
    """Every path research-runner's code uses outside the repo, under the mount it needs."""
    import hostrpc
    from research import job
    from sites.build import LOCK, Builder

    home, storage = tmp_path / "home", tmp_path / "storage"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ANYTHINGLLM_STORAGE", str(storage))
    for var in (
        "ANYTHINGLLM_ENV",
        "SITES_CONTENT",
        "SITES_OUTPUT",
        "RESEARCH_SOCKET",
        "SANDBOX_SOCKET",
    ):
        monkeypatch.delenv(var, raising=False)
    settings, builder = job.Settings.from_env(), Builder.from_env()
    site = job.Request("q").site

    def read_only(path: Path) -> bool:
        """Whether the mount `path` is under is read-only; fails if it isn't mounted."""
        path = str(path).replace(str(home), "%h").replace(str(storage), STORAGE)
        found = [t for t in RESEARCH if path == t or path.startswith(t + "/")]
        assert found, f"{path} isn't mounted"
        return RESEARCH[found[0]]

    for path in (
        settings.runlogs,
        settings.reports_dir,
        settings.documents_dir / "deep-research",
        builder.content / site / "reports",
        builder.content / LOCK,
        builder.output / f".{site}.new",
        builder.output / site,
        builder.output / "_cards",
        hostrpc.socket_path("research", "RESEARCH_SOCKET"),
    ):
        assert not read_only(path), path
    for path in (settings.env_file, hostrpc.socket_path("sandbox", "SANDBOX_SOCKET")):
        read_only(path)  # mounted; read-only will do
    # The research site is built in the sandbox, so the container needs no zola.
    assert builder.theme_from(site) == "system" and builder.remote is not None


def test_research_reaches_anythingllm_and_searxng_through_the_proxy(egress):
    keys = container_keys(QUADLET / "research-runner.container.in")
    env = dict(e.partition("=")[::2] for e in keys["Environment"])
    assert env["LIVE_HOST"] == "0.0.0.0"
    profile = egress.profile_for(egress.ips()["research-runner"])
    assert profile is not None and profile.name == "research"
    for key in ("ANYTHINGLLM_API", "SEARXNG_URL"):
        url = urlsplit(env[key].replace("@PUBLIC_HOST@", "host.example.ts.net"))
        assert url.scheme == "https" and url.hostname and url.port, key
        assert profile.judge(url.hostname, url.port) == "allow", key
