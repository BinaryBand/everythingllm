"""hostctl.prompt's check, and what deploy does with the block (sync's default prompt and
version variable), with AnythingLLM's API faked. The block itself is hostenv.prompt's."""

import importlib

import pytest
from hostctl import prompt as checks
from hostctl import units
from hostenv import prompt


def test_check_notes_workspaces_behind_or_without_a_block(monkeypatch, capsys):
    current = prompt.version(prompt.REPO_PROMPT.read_text())
    workspaces = [
        {"slug": "a", "openAiPrompt": prompt.block(prompt.REPO_PROMPT.read_text())},
        {"slug": "b", "openAiPrompt": prompt.block("older")},
        {"slug": "c", "openAiPrompt": None},
        {"slug": "agents-worker", "openAiPrompt": "a role's own"},
    ]
    from hostctl import machine

    monkeypatch.setattr(machine, "api", lambda *a, **k: {"workspaces": workspaces})
    checks.check()
    out = capsys.readouterr().out
    assert f"b: prompt is version {prompt.version('older')}, current {current}" in out
    assert "c: no EverythingLLM block" in out
    assert " a:" not in out and "agents-worker" not in out


class FakeAPI:
    """AnythingLLM's internal API, as much as deploy's prompt steps use."""

    def __init__(self, default="", variables=()):
        self.default = default
        self.variables = list(variables)
        self.calls = []

    def __call__(self, method, path, body=None, fresh=False):
        self.calls.append((method, path))
        if path == "/system/default-system-prompt":
            if method == "POST":
                self.default = body["defaultSystemPrompt"]
            return {"defaultSystemPrompt": self.default}
        if path == "/system/prompt-variables" and method == "GET":
            return {
                "variables": [{"key": "datetime", "type": "system"}, *self.variables]
            }
        if path.startswith("/system/prompt-variables"):
            return {"success": True}
        if path == "/scheduled-jobs":
            return {"jobs": []}
        raise AssertionError(f"deploy shouldn't call {method} {path}")


@pytest.fixture
def sync(monkeypatch, tmp_path):
    monkeypatch.setattr(units, "storage", lambda: tmp_path)
    from hostctl import sync

    sync = importlib.reload(sync)  # STORAGE is read on import
    monkeypatch.setattr(sync, "unset_skills", dict)
    monkeypatch.setattr(sync, "planned_files", lambda unset: {})
    monkeypatch.setattr(sync, "planned_removals", lambda unset: {})
    return sync


def test_deploy_sets_the_default_and_creates_the_variable_and_touches_no_workspace(
    sync, monkeypatch
):
    api = FakeAPI(default="An old default.")
    monkeypatch.setattr(sync, "api", api)
    sync.deploy()
    assert api.default == sync.repo_block()
    assert ("POST", "/system/prompt-variables") in api.calls
    assert not any("workspace" in path for _, path in api.calls)
    api.calls.clear()
    api.variables = [
        {
            "id": 7,
            "key": "everythingllm_version",
            "value": prompt.version(prompt.REPO_PROMPT.read_text()),
        }
    ]
    assert sync.planned_default() is None and sync.planned_variable() is None


def test_deploy_updates_a_stale_variable_in_place(sync, monkeypatch):
    api = FakeAPI(variables=[{"id": 7, "key": "everythingllm_version", "value": "0ld"}])
    monkeypatch.setattr(sync, "api", api)
    live, value = sync.planned_variable()
    assert live["id"] == 7 and value == prompt.version(prompt.REPO_PROMPT.read_text())
    sync.write_variable(live, value)
    assert api.calls[-1] == ("PUT", "/system/prompt-variables/7")
