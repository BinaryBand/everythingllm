import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sites import server, tools
from sites.store import SiteStore

CONFIG = 'base_url = "https://pages.example/news"\ntitle = "Daily News"\n'


@pytest.fixture
def store(tmp_path, monkeypatch):
    site = tmp_path / "src" / "news"
    (site / "content" / "editions").mkdir(parents=True)
    (site / "content" / "editions" / "_index.md").write_text("+++\n+++\n")
    (site / "zola.toml").write_text(CONFIG)
    (tmp_path / "content").mkdir()
    s = SiteStore(tmp_path / "src", tmp_path / "content")
    monkeypatch.setattr(tools, "_store", s)
    monkeypatch.setattr(tools, "_site_dir", tmp_path / "site")
    return s


def test_today_is_the_users_date_not_utcs(monkeypatch):
    # 23:00 UTC is already the next day in Stockholm.
    assert tools.today(datetime(2026, 10, 4, 23, tzinfo=timezone.utc)) == "2026-10-05"


def test_list_entries_leads_with_today_and_caps_the_list(store, monkeypatch):
    monkeypatch.setattr(tools, "today", lambda: "2026-10-04")
    assert tools.list_entries("news", "editions").splitlines() == [
        "Today is 2026-10-04 in the user's time zone.",
        "No entries yet.",
    ]
    for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
        store.write("news", "editions", day, f"Edition {day}", day)
    lines = tools.list_entries("news", "editions", limit=2).splitlines()
    assert lines[0] == "Today is 2026-10-04 in the user's time zone."
    assert [l.split()[1] for l in lines[1:3]] == [
        "editions/2026-10-03",
        "editions/2026-10-02",
    ]
    assert lines[3] == "(1 older not shown; raise limit to see them.)"
    lines = tools.list_entries("news", "editions", limit=0).splitlines()
    assert len(lines) == 5  # today, three entries and the newest's card
    assert lines[4].startswith("Card for the newest: [![Edition 2026-10-03](")


def test_the_server_explains_a_missing_runner(monkeypatch, tmp_path):
    monkeypatch.setenv("SITES_SOCKET", str(tmp_path / "nope.sock"))
    with pytest.raises(server.ToolError, match="sites runner isn't running"):
        asyncio.run(server.list_sites())


def test_every_tool_is_an_op_of_the_runner():
    from sites import tools

    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == {f.__name__ for f in tools.OPS} - {f.__name__ for f in tools.SKILLS}


def test_the_ops_that_write_are_skills():
    """Each op in SKILLS has its skill, which sends that op (anythingllm/agent-skills/<op>)."""
    from sites import tools

    skills = Path(__file__).resolve().parents[3] / "anythingllm" / "agent-skills"
    for op in tools.SKILLS:
        handler = skills / op.__name__.replace("_", "-") / "handler.js"
        assert f'op: "{op.__name__}"' in handler.read_text(), op.__name__
