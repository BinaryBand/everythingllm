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
    the runners that are."""
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
    monkeypatch.setattr(sync.units, "enabled", lambda unit: unit in enabled)
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
