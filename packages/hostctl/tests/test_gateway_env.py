import sys

import pytest
from hostctl import gateway_env


def run(monkeypatch, path):
    monkeypatch.setattr(sys, "argv", ["gateway_env", str(path)])
    gateway_env.main()


def test_makes_the_file_with_a_token_it_never_prints(monkeypatch, tmp_path, capsys):
    path = tmp_path / "conf" / "gateway.env"
    run(monkeypatch, path)
    assert path.stat().st_mode & 0o777 == 0o600
    [line] = [
        l for l in path.read_text().splitlines() if l.startswith("GATEWAY_TOKEN_")
    ]
    name, token = line.split("=", 1)
    assert name == "GATEWAY_TOKEN_CLAUDE_CODE" and len(token) > 30
    out = capsys.readouterr().out
    assert "claude-code" in out and token not in out
    run(monkeypatch, path)  # a second run keeps the file
    assert line in path.read_text()


def test_a_file_without_a_token_stops_the_setup(monkeypatch, tmp_path):
    path = tmp_path / "gateway.env"
    path.write_text("# GATEWAY_TOKEN_OLD=revoked\nGATEWAY_TOKEN_EMPTY=\n")
    with pytest.raises(SystemExit, match="add a GATEWAY_TOKEN_"):
        run(monkeypatch, path)
