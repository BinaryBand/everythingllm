"""hostctl.cli: the commands `uv run hostctl` runs, with subprocesses and the other hostctl
modules faked, so each test reads as the steps a command takes, in order."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from hostctl import cli

# The commands that aren't per app.
TARGETS = [
    "install",
    "units",
    "diff",
    "deploy",
    "skills",
    "skills-check",
    "restart",
    "logs",
    "status",
    "health",
    "test",
    "test-skills",
    "mcp-sync",
    "apps",
    "routes",
    "gateway-client",
    "sandbox-images",
    "service-images",
    "browser-images",
    "browser-reset",
    "sites-build",
]


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
    sync = SimpleNamespace(
        main=step("sync"), mcp_packages=lambda: ["sites", "research"]
    )
    monkeypatch.setitem(sys.modules, "hostctl.sync", sync)
    monkeypatch.setattr("hostctl.sync", sync, raising=False)
    monkeypatch.setenv("ANYTHINGLLM_STORAGE", "/storage")
    return calls


def test_every_target_is_a_command():
    for target in TARGETS:
        cli.lookup(target)
    assert cli.lookup("sites-setup") == (cli.setup_app, ["sites"])
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


def test_deploy_refuses_a_worktree(ran, monkeypatch, tmp_path):
    (tmp_path / ".git").write_text("gitdir: /elsewhere\n")
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="is a git worktree"):
        cli.main(["deploy"])
    assert ran == []


def test_diff_checks_the_skills_first(ran):
    cli.main(["diff"])
    assert ran[1:] == ["sync diff", "units diff"]


def test_an_app_setup_installs_the_units_first(ran):
    cli.main(["sites-setup"])
    assert ran == ["units install sites", "appctl setup sites"]


def test_install_keeps_going_past_a_failed_health_check(ran, monkeypatch):
    def health():
        ran.append("health")
        raise SystemExit(1)

    monkeypatch.setattr(cli, "health", health)
    cli.main(["install"])
    assert ran[:3] == ["machine check", "units install", "machine wait-api"]
    assert ran[-3:] == ["machine wait-api", "health", "machine checklist"]
    assert "appctl setup --installed" in ran


def test_sandbox_images_builds_the_image_and_egress_net(ran):
    cli.main(["sandbox-images"])
    folder = cli.ROOT / "host" / "containers" / "sandbox"
    assert ran == [
        f"podman build -t localhost/everythingllm-sandbox -f {folder}/Containerfile.sandbox {folder}",
        "podman network exists egress-net",
        (
            "podman network create --internal --disable-dns --subnet 10.89.79.0/24"
            " --ip-range 10.89.79.128/25 egress-net"
        ),
    ]


def test_service_images_builds_the_image_and_egress_net(ran):
    cli.main(["service-images"])
    folder = cli.ROOT / "host" / "containers" / "service"
    assert ran == [
        f"podman build -t localhost/everythingllm-service -f {folder}/Containerfile {folder}",
        "podman network exists egress-net",
        (
            "podman network create --internal --disable-dns --subnet 10.89.79.0/24"
            " --ip-range 10.89.79.128/25 egress-net"
        ),
    ]


def egress_net(monkeypatch, subnets, rm=0):
    """Fake podman with egress-net already there, inspected as `subnets`."""
    calls = []

    def run(cmd, **kw):
        calls.append(" ".join(cmd))
        out = json.dumps([{"subnets": subnets}]) if "inspect" in cmd else ""
        return SimpleNamespace(returncode=rm if "rm" in cmd else 0, stdout=out)

    monkeypatch.setattr(cli.subprocess, "run", run)
    return calls


def test_service_images_leaves_an_egress_net_made_as_asked(monkeypatch):
    lease = {"start_ip": "10.89.79.129", "end_ip": "10.89.79.254"}
    calls = egress_net(monkeypatch, [{"subnet": "10.89.79.0/24", "lease_range": lease}])
    cli.main(["service-images"])
    assert not [c for c in calls if "network create" in c or "network rm" in c]


def test_service_images_makes_again_an_egress_net_without_its_ip_range(monkeypatch):
    """Made before ip_range was, podman would give a stray container any address, a
    stopped service's among them, and with it that service's egress profile."""
    calls = egress_net(monkeypatch, [{"subnet": "10.89.79.0/24"}])
    cli.main(["service-images"])
    assert calls[-2:] == [
        "podman network rm egress-net",
        (
            "podman network create --internal --disable-dns --subnet 10.89.79.0/24"
            " --ip-range 10.89.79.128/25 egress-net"
        ),
    ]
    egress_net(monkeypatch, [{"subnet": "10.89.79.0/24"}], rm=2)  # in use
    with pytest.raises(SystemExit, match="in use"):
        cli.main(["service-images"])


def test_import_takes_a_name_and_needs_one(ran):
    cli.main(["import-skill", "weather"])
    assert ran == ["sync import-skill weather"]
    with pytest.raises(SystemExit, match="import-skill"):
        cli.main(["import-skill"])


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


def test_browser_images_builds_the_image_and_puts_its_novnc_in_place(
    ran, monkeypatch, tmp_path
):
    monkeypatch.setattr(cli.run_guard, "DATA", tmp_path)
    old = tmp_path / "browser" / "novnc"
    old.mkdir(parents=True)
    (old / "stale.js").write_text("")
    fake = cli.subprocess.run

    def run(cmd, **kw):
        if list(cmd[:2]) == ["podman", "cp"]:  # as podman would: the image's /opt/novnc
            (Path(cmd[3]) / "core").mkdir(parents=True)
        return fake(cmd, **kw)

    monkeypatch.setattr(cli.subprocess, "run", run)
    cli.main(["browser-images"])
    assert ran[0].startswith("podman build -t localhost/everythingllm-browser -f ")
    assert any(
        c.startswith(
            "podman network create --internal --disable-dns --subnet 10.89.79.0/24"
        )
        for c in ran
    )
    copy = "everythingllm-browser-novnc-copy"
    assert ran[-3:] == [
        f"podman create --name {copy} localhost/everythingllm-browser",
        f"podman cp {copy}:/opt/novnc {tmp_path}/browser/.novnc.new",
        f"podman rm -f {copy}",
    ]
    assert sorted(p.name for p in (tmp_path / "browser").iterdir()) == ["novnc"]
    assert (old / "core").is_dir() and not (old / "stale.js").exists()


def test_browser_reset_stops_the_browser_and_wipes_only_its_profile(
    ran, monkeypatch, tmp_path
):
    monkeypatch.setattr(cli.run_guard, "DATA", tmp_path)
    home = tmp_path / "sandbox" / "workspaces" / "career"
    (home / "browser" / "profile").mkdir(parents=True)
    (home / "project").mkdir()
    cli.main(["browser-reset", "career"])
    assert ran == ["podman rm -f --time 5 everythingllm-browser-career"]
    assert not (home / "browser").exists() and (home / "project").is_dir()
    for bad in ("../career", "Career", "career\n"):
        with pytest.raises(SystemExit):
            cli.main(["browser-reset", bad])
