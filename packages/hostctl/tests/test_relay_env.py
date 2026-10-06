import sys

from hostctl import relay_env


def run(monkeypatch, path):
    monkeypatch.setattr(sys, "argv", ["relay_env", str(path)])
    relay_env.main()


def test_makes_the_file_with_only_the_ntfy_settings(monkeypatch, tmp_path):
    path = tmp_path / "conf" / "relay.env"
    run(monkeypatch, path)
    assert path.stat().st_mode & 0o777 == 0o600
    keys = [
        l.split("=")[0]
        for l in path.read_text().splitlines()
        if "=" in l and l[0] != "#"
    ]
    assert keys == ["NTFY_URL", "NTFY_TOKEN"]
    path.write_text("NTFY_URL=https://ntfy.example/t\n")
    path.chmod(0o644)
    run(monkeypatch, path)  # a second run keeps the file, needing nothing filled in
    assert path.read_text() == "NTFY_URL=https://ntfy.example/t\n"
    assert path.stat().st_mode & 0o777 == 0o600
