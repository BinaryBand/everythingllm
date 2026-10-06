import sys

from hostctl import relay_env


def run(monkeypatch, path):
    monkeypatch.setattr(sys, "argv", ["relay_env", str(path)])
    relay_env.main()


def test_makes_the_file_with_only_the_ntfy_settings(monkeypatch, tmp_path, capsys):
    path = tmp_path / "conf" / "relay.env"
    run(monkeypatch, path)
    assert path.stat().st_mode & 0o777 == 0o600
    keys = [
        l.split("=")[0]
        for l in path.read_text().splitlines()
        if "=" in l and l[0] != "#"
    ]
    assert keys == ["NTFY_URL", "NTFY_TOKEN"]
    run(monkeypatch, path)  # a second run keeps the file and needs nothing filled in
    assert "no longer read" not in capsys.readouterr().out


def test_an_old_file_passes_with_a_note_and_isnt_changed(monkeypatch, tmp_path, capsys):
    path = tmp_path / "relay.env"
    old = "ANYTHINGLLM_API_KEY=k-secret\nRELAY_TOKEN=t-secret\nNTFY_URL=\n"
    path.write_text(old)
    path.chmod(0o644)
    run(monkeypatch, path)
    out = capsys.readouterr().out
    assert "ANYTHINGLLM_API_KEY and RELAY_TOKEN" in out and "no longer read" in out
    assert "k-secret" not in out and "t-secret" not in out
    assert path.read_text() == old and path.stat().st_mode & 0o777 == 0o600
