import pytest


@pytest.fixture(autouse=True)
def no_host_settings(monkeypatch):
    """Tests run the same on any machine: none of this one's host.env (which `uv run hostctl test`
    exports, and which the site builds would find at the repo root) reaches them."""
    monkeypatch.delenv("PUBLIC_HOST", raising=False)
    monkeypatch.delenv("ANYTHINGLLM_STORAGE", raising=False)
    # Nor does any test reach this machine's sandbox runner (site builds would ask it).
    monkeypatch.setenv("SANDBOX_SOCKET", "/nonexistent/sandbox/runner.sock")
    try:
        from sites import store
    except ImportError:  # a package that doesn't use the sites
        return
    monkeypatch.setattr(store, "host_file", lambda source: source / "no-host.env")
