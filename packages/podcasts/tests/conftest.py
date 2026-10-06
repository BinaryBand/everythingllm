import shutil
from collections import namedtuple

import pytest
from podcasts.library import Library

Usage = namedtuple("Usage", "total used free")


@pytest.fixture(autouse=True)
def not_this_machines_storage(tmp_path, monkeypatch):
    """Storage defaults to this machine's (/srv/anythingllm/storage, with the real model
    key); Library.from_env and the real sync use a scratch folder instead."""
    monkeypatch.setenv("ANYTHINGLLM_STORAGE", str(tmp_path / "storage"))
    monkeypatch.delenv("ANYTHINGLLM_ENV", raising=False)


@pytest.fixture(autouse=True)
def free_disk(monkeypatch):
    """Sets the free disk space the library sees; plenty, as pytest's tmp_path is often a
    small tmpfs, under the download floor."""

    def free(n: int) -> None:
        monkeypatch.setattr(shutil, "disk_usage", lambda path: Usage(10**13, 0, n))

    free(10**13)
    return free


@pytest.fixture
def lib(tmp_path):
    return Library(
        tmp_path / "site", tmp_path / "state", "https://host.ts.net:8445/podcasts"
    )
