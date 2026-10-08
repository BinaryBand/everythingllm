import json
from pathlib import Path

import hostenv
import pytest


def test_storage_comes_from_host_env(monkeypatch):
    monkeypatch.setenv("ANYTHINGLLM_STORAGE", "/x/storage")
    assert hostenv.storage() == Path("/x/storage")
    monkeypatch.delenv("ANYTHINGLLM_STORAGE")
    assert hostenv.storage() == Path("/srv/anythingllm/storage")


def test_socket_path_is_the_env_or_storage(monkeypatch):
    monkeypatch.setenv("ANYTHINGLLM_STORAGE", "/data/allm")
    monkeypatch.delenv("PICKY_SOCKET", raising=False)
    assert hostenv.socket_path("picky", "PICKY_SOCKET") == Path(
        "/data/allm/everythingllm/picky/runner.sock"
    )
    monkeypatch.setenv("PICKY_SOCKET", "/tmp/p.sock")
    assert hostenv.socket_path("picky", "PICKY_SOCKET") == Path("/tmp/p.sock")


def test_env_values_keeps_only_the_names_and_lets_the_environment_win(
    tmp_path, monkeypatch
):
    env = tmp_path / ".env"
    env.write_text("A='one'\n# c\nB = \"two\"\nSECRET=x\nA=last\n")
    monkeypatch.setenv("B", "env")
    monkeypatch.delenv("C", raising=False)
    assert hostenv.env_values(env, ["A", "B", "C"]) == {"A": "last", "B": "env"}
    assert hostenv.env_values(env, ["B"], environ=False) == {"B": "two"}
    assert hostenv.env_values(tmp_path / "missing", ["A"]) == {}


@pytest.fixture
def anythingllm(monkeypatch):
    """A stand-in for AnythingLLM's /api/request-token: the logins it saw, and its password."""
    import http.server
    import threading

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, body))
            ok = body.get("password") == "s3cret!"
            self.send_response(200 if ok else 401)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"valid": ok, "token": f"jwt{len(seen)}" if ok else None}
                ).encode()
            )

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(hostenv, "_tokens", {})
    yield f"http://127.0.0.1:{server.server_address[1]}/api", seen
    server.shutdown()


def test_no_password_means_no_login(anythingllm, tmp_path):
    api, seen = anythingllm
    env = tmp_path / ".env"
    env.write_text("AUTH_TOKEN='s3cret!'\n")  # without JWT_SECRET it isn't protected
    assert hostenv.anythingllm_headers(api, env) == {}
    assert hostenv.anythingllm_headers(api, tmp_path / "missing") == {}
    assert seen == []


def test_logs_in_once_and_again_when_fresh(anythingllm, tmp_path):
    api, seen = anythingllm
    env = tmp_path / ".env"
    env.write_text("AUTH_TOKEN='s3cret!'\nJWT_SECRET=abc\n")
    assert hostenv.anythingllm_headers(api, env) == {"Authorization": "Bearer jwt1"}
    assert hostenv.anythingllm_headers(api, env) == {"Authorization": "Bearer jwt1"}
    assert seen == [("/api/request-token", {"password": "s3cret!"})]
    assert hostenv.anythingllm_headers(api, env, fresh=True) == {
        "Authorization": "Bearer jwt2"
    }


def test_a_refused_password_says_so_without_showing_it(anythingllm, tmp_path):
    api, _ = anythingllm
    env = tmp_path / ".env"
    env.write_text("AUTH_TOKEN=wrong-one\nJWT_SECRET=abc\n")
    with pytest.raises(hostenv.LoginFailed, match="refused the password") as e:
        hostenv.anythingllm_headers(api, env)
    assert "wrong-one" not in str(e.value)


def test_the_live_cards_are_on_the_public_hosts_pages_site(monkeypatch):
    monkeypatch.setenv("PUBLIC_HOST", "box.tail.ts.net")
    assert hostenv.pages_url() == "https://box.tail.ts.net:8445/"
    monkeypatch.delenv("PUBLIC_HOST")
    assert hostenv.pages_url() == ""
