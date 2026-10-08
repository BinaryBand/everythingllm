import importlib

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
