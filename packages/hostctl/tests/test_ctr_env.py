import pytest
from hostctl import ctr_env

ENV = """\
# AnythingLLM's .env
DEEPSEEK_API_KEY='sk-deep'
DEEPSEEK_MODEL_PREF=deepseek-chat
OPENROUTER_API_KEY=sk-router
AUTH_TOKEN="hunter2"
JWT_SECRET=signing-secret
ZAI_API_KEY=
"""


def test_a_container_gets_only_its_keys(tmp_path, capsys):
    source = tmp_path / "storage" / ".env"
    source.parent.mkdir()
    source.write_text(ENV)
    out = tmp_path / "conf" / "ctr" / "research-runner.env"
    ctr_env.main(
        [
            str(source),
            str(out),
            "DEEPSEEK_API_KEY",
            "DEEPSEEK_MODEL_PREF",
            "ZAI_API_KEY",
            "AUTH_TOKEN",
            "JWT_SECRET?",
        ]
    )
    # Quotes dropped as every reader drops them; an empty key left out; the signing secret
    # only as being set.
    assert out.read_text() == (
        "DEEPSEEK_API_KEY=sk-deep\nDEEPSEEK_MODEL_PREF=deepseek-chat\n"
        "AUTH_TOKEN=hunter2\nJWT_SECRET=set\n"
    )
    assert "sk-router" not in out.read_text()
    assert out.stat().st_mode & 0o777 == 0o600
    assert out.parent.stat().st_mode & 0o777 == 0o700
    # Replaced whole on the next start, so a changed or removed key follows.
    source.write_text("DEEPSEEK_API_KEY=sk-new\n")
    ctr_env.main([str(source), str(out), "DEEPSEEK_API_KEY", "AUTH_TOKEN"])
    assert out.read_text() == "DEEPSEEK_API_KEY=sk-new\n"
    assert [p.name for p in out.parent.iterdir()] == ["research-runner.env"]


def test_an_unreadable_env_gives_an_empty_file(tmp_path, capsys):
    out = tmp_path / "ctr" / "sites-runner.env"
    ctr_env.main([str(tmp_path / "missing"), str(out), "DEEPSEEK_API_KEY"])
    assert out.read_text() == ""
    assert "nothing read" in capsys.readouterr().out


def test_bad_arguments(tmp_path):
    with pytest.raises(SystemExit):
        ctr_env.main([str(tmp_path / ".env"), str(tmp_path / "out")])
    with pytest.raises(ValueError, match="not a key"):
        ctr_env.share({}, ["A=B"])


def test_hostrpc_reads_the_share_as_it_read_the_env(tmp_path):
    """What the containers' code reads the file with: the password logs in only when
    JWT_SECRET is set, which `set` keeps true."""
    import hostrpc

    out = tmp_path / "x.env"
    out.write_text(
        ctr_env.share(
            {"AUTH_TOKEN": "pw", "JWT_SECRET": "s"}, ["AUTH_TOKEN", "JWT_SECRET?"]
        )
    )
    assert hostrpc.env_values(out, ("AUTH_TOKEN", "JWT_SECRET"), environ=False) == {
        "AUTH_TOKEN": "pw",
        "JWT_SECRET": "set",
    }
