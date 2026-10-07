import importlib

import pytest
from hostctl import jobs, units


def test_the_repo_manages_the_daily_news_page():
    found = jobs.repo_jobs()
    assert "Daily News Page" in found
    assert found["Daily News Page"]["prompt"] and found["Daily News Page"]["schedule"]


def test_job_tools_reads_anythingllms_json_text():
    assert jobs.job_tools({"tools": '["web-browsing"]'}) == ["web-browsing"]
    for tools in (None, "", "not json", '{"a": 1}'):
        assert jobs.job_tools({"tools": tools}) is None


@pytest.fixture
def sync(monkeypatch, tmp_path):
    monkeypatch.setattr(units, "storage", lambda: tmp_path)
    from hostctl import sync

    return importlib.reload(sync)  # STORAGE is read on import


def test_deploy_wont_match_a_repo_job_two_live_jobs_share_a_name(sync, monkeypatch):
    live = [
        {"id": 1, "name": "Daily News Page", "tools": None},
        {"id": 2, "name": "Mine", "tools": '["web-browsing"]'},
        {"id": 3, "name": "Mine", "tools": None},
    ]
    monkeypatch.setattr(
        sync, "api", lambda method, path: {"jobs": [dict(j) for j in live]}
    )
    found = sync.live_jobs(["Daily News Page"])  # another name's twins don't matter
    assert found["Daily News Page"]["id"] == 1 and found["Mine"]["tools"] == []
    live.append({"id": 4, "name": "Daily News Page", "tools": None})
    with pytest.raises(
        SystemExit, match="more than one scheduled job named 'Daily News Page'"
    ):
        sync.live_jobs(["Daily News Page"])
    with pytest.raises(SystemExit, match="'Mine'"):
        sync.live_jobs(["Mine"])
