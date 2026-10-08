import sys

import pytest
from hostctl import gateway_env
from hostctl.units import env_file


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


# --- gateway-client ---


@pytest.fixture
def grants(tmp_path):
    path = tmp_path / "grants.toml"
    path.write_text('[clients.laptop]\ntools = ["agents", "sandbox"]\n')
    return path


def add(path, grants, name="laptop"):
    gateway_env.add_client(name, path, grants)


def tokens(path):
    return {k: v for k, v in env_file(path).items() if k.startswith(gateway_env.PREFIX)}


def test_a_client_gets_a_token_and_the_command_to_use_it(
    monkeypatch, tmp_path, grants, capsys
):
    monkeypatch.setenv("PUBLIC_HOST", "box.tail.ts.net")
    path = tmp_path / "gateway.env"
    path.write_text("GATEWAY_TOKEN_CLAUDE_CODE=theirs")  # no newline at the end
    path.chmod(0o644)
    add(path, grants)
    found = tokens(path)
    token = found.pop("GATEWAY_TOKEN_LAPTOP")
    assert found == {"GATEWAY_TOKEN_CLAUDE_CODE": "theirs"} and len(token) > 30
    assert path.stat().st_mode & 0o777 == 0o600
    out = capsys.readouterr().out
    assert (
        "claude mcp add --transport http everythingllm "
        "https://box.tail.ts.net:8452/mcp "
        f"--header 'Authorization: Bearer {token}'"
    ) in out
    assert "theirs" not in out  # only this client's token
    assert "Its grant in grants.toml: agents, sandbox." in out
    assert "systemctl --user restart gateway" in out


def test_a_client_with_a_token_keeps_it(tmp_path, grants, capsys):
    path = tmp_path / "gateway.env"
    path.write_text("GATEWAY_TOKEN_LAPTOP=kept\n")
    add(path, grants)
    assert path.read_text() == "GATEWAY_TOKEN_LAPTOP=kept\n"
    out = capsys.readouterr().out
    assert "already has a token for laptop" in out
    assert "Bearer kept" in out
    assert "https://<PUBLIC_HOST>:8452/mcp" in out  # PUBLIC_HOST is cleared in tests


def test_gateway_client_makes_the_file_when_theres_none(tmp_path, grants, capsys):
    path = tmp_path / "conf" / "gateway.env"
    add(path, grants, "phone")
    assert list(tokens(path)) == ["GATEWAY_TOKEN_PHONE"]
    assert path.read_text().startswith("# The MCP gateway's client tokens")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    out = capsys.readouterr().out
    assert "phone has no grant, so it gets no tools" in out
    assert "[clients.phone]" in out


@pytest.mark.parametrize("name", ["Laptop", "my_laptop", "-x", "x-", "", "a" * 64])
def test_gateway_client_refuses_a_name_the_gateway_wouldnt_take(tmp_path, name):
    path = tmp_path / "gateway.env"
    with pytest.raises(SystemExit, match="isn't a client name"):
        add(path, tmp_path / "grants.toml", name)
    assert not path.exists()


def test_gateway_client_leaves_an_empty_line_to_the_user(tmp_path, grants):
    path = tmp_path / "gateway.env"
    path.write_text("GATEWAY_TOKEN_LAPTOP=\n")
    with pytest.raises(SystemExit, match="empty GATEWAY_TOKEN_LAPTOP= line"):
        add(path, grants)
    assert path.read_text() == "GATEWAY_TOKEN_LAPTOP=\n"


def test_a_clients_key_is_its_name_as_the_gateway_reads_it():
    assert gateway_env.key("claude-code") == "GATEWAY_TOKEN_CLAUDE_CODE"
    assert gateway_env.key("pi-2") == "GATEWAY_TOKEN_PI_2"


def test_the_repos_grants_name_claude_code():
    assert "sandbox" in gateway_env.granted("claude-code")
    assert gateway_env.granted("nobody") is None
