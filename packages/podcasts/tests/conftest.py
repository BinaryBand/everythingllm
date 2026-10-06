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
def not_this_machines_host_env(tmp_path, monkeypatch):
    """The workers read PODCASTS_QUIET_HOURS and PODCASTS_TRANSCRIBE_THREADS from the
    repo's host.env each time; a test reads a scratch one, so this machine's quiet hours
    don't change what a test run at night sees."""
    from podcasts import transcripts, worker

    monkeypatch.setattr(worker, "HOST_ENV", tmp_path / "host.env")
    monkeypatch.setattr(transcripts, "HOST_ENV", tmp_path / "host.env")
    monkeypatch.delenv("PODCASTS_QUIET_HOURS", raising=False)


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
