import importlib
import json

import pytest
from hostctl import units


@pytest.fixture
def sync(monkeypatch, tmp_path):
    monkeypatch.setattr(units, "storage", lambda: tmp_path)
    from hostctl import sync

    return importlib.reload(sync)  # STORAGE is read on import


def test_deploy_writes_through_no_symlink_the_container_left(tmp_path):
    target = tmp_path / "outside"
    target.write_text("mine")
    dest = tmp_path / "skills" / "s" / "handler.js"
    dest.parent.mkdir(parents=True)
    (dest.parent / ".handler.js.tmp").symlink_to(target)  # the old fixed temp name
    units.replace_file(dest, "new")
    assert dest.read_text() == "new" and target.read_text() == "mine"
    assert dest.stat().st_mode & 0o777 == 0o644
    dest.unlink()
    dest.symlink_to(target)
    units.replace_file(dest, "again")
    assert not dest.is_symlink() and target.read_text() == "mine"


def test_import_skill_refuses_a_skill_holding_symlinks(sync, monkeypatch, tmp_path):
    live = tmp_path / "live"
    (live / "s").mkdir(parents=True)
    (live / "s" / "handler.js").write_text("x")
    (live / "s" / "secret").symlink_to(tmp_path / "gateway.env")
    monkeypatch.setattr(sync, "LIVE_SKILLS", live)
    monkeypatch.setattr(sync, "REPO", tmp_path / "repo")
    with pytest.raises(SystemExit, match="symlinks"):
        sync.import_skill("s")
    assert not (tmp_path / "repo" / "agent-skills" / "s").exists()


def skills_repo(sync, monkeypatch, tmp_path, enabled):
    """A repo with a sandbox skill and a browser skill, and a live copy of each; `enabled`
    the runners that are (None: ask systemctl)."""
    for skill in ("run-code", "browse"):
        folder = tmp_path / "repo" / "agent-skills" / skill
        folder.mkdir(parents=True)
        (folder / "plugin.json").write_text(json.dumps({"name": skill}))
        (folder / "handler.js").write_text("// " + skill)
        live = tmp_path / "live" / skill
        live.mkdir(parents=True)
        (live / "handler.js").write_text("old")
    monkeypatch.setattr(sync, "REPO", tmp_path / "repo")
    monkeypatch.setattr(sync, "LIVE_SKILLS", tmp_path / "live")
    monkeypatch.setattr(sync, "KEPT", tmp_path / "kept")
    if enabled is not None:
        monkeypatch.setattr(sync.units, "enabled", lambda units: set(units) & enabled)
    for name in ("planned_default", "planned_variable"):
        monkeypatch.setattr(sync, name, lambda: None)


def test_deploy_copies_only_the_skills_of_apps_set_up_here(sync, monkeypatch, tmp_path):
    skills_repo(sync, monkeypatch, tmp_path, {"browser-runner.service"})
    assert sync.unset_skills()["run-code"] == "sandbox"
    assert "browse" not in sync.unset_skills()
    sync.deploy()
    live = tmp_path / "live"
    assert (live / "browse" / "handler.js").read_text() == "// browse"
    assert not (live / "run-code").exists()  # the sandbox isn't set up here


def test_diff_says_which_skills_deploy_would_take_out(
    sync, monkeypatch, tmp_path, capsys
):
    skills_repo(sync, monkeypatch, tmp_path, {"sandbox-runner.service"})
    assert sync.diff() is True
    out = capsys.readouterr().out
    assert "remove live/live/browse: browser isn't set up here" in out
    assert "+// run-code" in out and "+// browse" not in out
    assert (tmp_path / "live" / "browse").is_dir()  # diff changes nothing


def test_a_skill_the_container_made_a_symlink_goes_as_the_link(
    sync, monkeypatch, tmp_path
):
    skills_repo(sync, monkeypatch, tmp_path, set())
    target = tmp_path / "outside"
    target.mkdir()
    (target / "keep").write_text("mine")
    live = tmp_path / "live" / "browse"
    for f in live.iterdir():
        f.unlink()
    live.rmdir()
    live.symlink_to(target)
    sync.deploy()
    assert not live.is_symlink() and not live.exists()
    assert (target / "keep").read_text() == "mine"
    assert not (tmp_path / "live" / "run-code").exists()


def test_deploy_stops_when_systemctl_cant_say_whats_enabled(
    sync, monkeypatch, tmp_path
):
    skills_repo(sync, monkeypatch, tmp_path, None)
    no_bus = units.subprocess.CompletedProcess([], 1, "", "Failed to connect to bus")
    monkeypatch.setattr(units.subprocess, "run", lambda *a, **k: no_bus)
    with pytest.raises(SystemExit, match="Failed to connect to bus"):
        sync.deploy()
    assert (tmp_path / "live" / "browse" / "handler.js").read_text() == "old"


def test_a_skill_taken_out_comes_back_as_the_ui_left_it(sync, monkeypatch, tmp_path):
    on: set[str] = set()
    skills_repo(sync, monkeypatch, tmp_path, on)
    repo = tmp_path / "repo" / "agent-skills" / "browse" / "plugin.json"
    repo.write_text(
        json.dumps({"name": "browse", "setup_args": {"KEY": {"type": "string"}}})
    )
    live = tmp_path / "live" / "browse" / "plugin.json"
    live.write_text(
        json.dumps({"active": False, "setup_args": {"KEY": {"value": "k"}}})
    )
    sync.deploy()  # the browser isn't set up: out it goes, and what the UI set is kept
    assert not live.parent.exists()
    assert (tmp_path / "kept" / "browse.json").stat().st_mode & 0o777 == 0o600
    on.add("browser-runner.service")
    sync.deploy()  # it's back as the user left it
    back = json.loads(live.read_text())
    assert back["active"] is False and back["setup_args"]["KEY"]["value"] == "k"
    assert not (tmp_path / "kept" / "browse.json").exists()
