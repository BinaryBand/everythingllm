"""hostctl.cli: the commands `uv run hostctl` runs, with subprocesses and the other hostctl
modules faked, so each test reads as the steps a command takes, in order."""

import sys
from types import SimpleNamespace

import pytest
from hostctl import cli

# The commands that aren't per app.
TARGETS = """install units diff deploy skills skills-check restart logs status health test
test-skills mcp-sync apps serve-setup gateway-client sandbox-images service-images
sites-build""".split()


@pytest.fixture
def ran(monkeypatch):
    calls = []

    def step(module):
        return lambda argv: calls.append(f"{module} {' '.join(argv)}")

    def run(cmd, **kw):
        calls.append(" ".join(cmd))
        return SimpleNamespace(returncode=1 if "exists" in cmd else 0)

    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli.units, "main", step("units"))
    monkeypatch.setattr(cli.machine, "main", step("machine"))
    monkeypatch.setattr(cli.appctl, "main", step("appctl"))
    sync = SimpleNamespace(main=step("sync"), mcp_packages=lambda: ["sites", "podcasts"])
    monkeypatch.setitem(sys.modules, "hostctl.sync", sync)
    monkeypatch.setattr("hostctl.sync", sync, raising=False)
    monkeypatch.setenv("ANYTHINGLLM_STORAGE", "/storage")
    return calls


def test_every_target_is_a_command():
    for target in TARGETS:
        cli.lookup(target)
    assert cli.lookup("podcasts-setup") == (cli.setup_app, ["podcasts"])
    assert cli.lookup("research-logs") == (cli.app_logs, ["research"])
    with pytest.raises(SystemExit):
        cli.lookup("nope-setup")
    with pytest.raises(SystemExit):
        cli.lookup("<app>-setup")


def test_deploy_checks_the_skills_first_and_ends_with_the_sites(ran):
    cli.main(["deploy"])
    assert ran[0].endswith("python -m hostctl.skills --check")
    assert ran[1] == "sync deploy"
    assert "--package sites --project /mcp" in ran[2]
    assert "--inexact" not in ran[2] and "--inexact" in ran[3]
    assert ran[4:] == [
        "systemctl --user restart anythingllm.service",
        "uv run --package sites sites-build",
    ]


def test_diff_checks_the_skills_first(ran):
    cli.main(["diff"])
    assert ran[1:] == ["sync diff", "units diff"]


def test_an_app_setup_installs_the_units_first(ran):
    cli.main(["podcasts-setup"])
    assert ran == ["units install", "appctl setup podcasts"]


def test_install_keeps_going_past_a_failed_health_check(ran, monkeypatch):
    def health():
        ran.append("health")
        raise SystemExit(1)

    monkeypatch.setattr(cli, "health", health)
    cli.main(["install"])
    assert ran[:3] == ["machine check", "units install", "machine wait-api"]
    assert ran[-3:] == ["machine wait-api", "health", "machine checklist"]
    assert "appctl setup --installed" in ran


def test_sandbox_images_creates_the_network_only_when_missing(ran):
    cli.main(["sandbox-images"])
    assert ran[-2:] == [
        "podman network exists sandbox-net",
        "podman network create --internal --disable-dns --subnet 10.89.77.0/24 sandbox-net",
    ]


def test_service_images_builds_the_image_and_egress_net(ran):
    cli.main(["service-images"])
    folder = cli.ROOT / "host" / "containers" / "service"
    assert ran == [
        f"podman build -t localhost/everythingllm-service -f {folder}/Containerfile {folder}",
        "podman network exists egress-net",
        "podman network create --internal --disable-dns --subnet 10.89.79.0/24"
        " --ip-range 10.89.79.128/25 egress-net",
    ]


def test_import_takes_a_name_and_needs_one(ran):
    cli.main(["import-job", "Daily News Page"])
    assert ran == ["sync import-job Daily News Page"]
    with pytest.raises(SystemExit, match="import-job"):
        cli.main(["import-job"])


def test_gateway_client_takes_the_clients_name(ran, monkeypatch):
    added = []
    monkeypatch.setattr(cli.gateway_env, "add_client", added.append)
    cli.main(["gateway-client", "laptop"])
    assert added == ["laptop"] and ran == []
    with pytest.raises(SystemExit, match="gateway-client <name>"):
        cli.main(["gateway-client"])


def test_test_runs_without_host_env(ran, monkeypatch):
    monkeypatch.delenv("ANYTHINGLLM_STORAGE")
    monkeypatch.setattr(cli, "ROOT", cli.ROOT / "no-such-dir")
    cli.main(["test"])
    assert ran[0] == "uv run --all-packages --all-extras pytest -q"
