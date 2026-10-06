import json
import re
from types import SimpleNamespace

import pytest
from research import job, publish
from research.llm import LLM
from sites.build import Builder
from sites.store import Entry
from test_pipeline import PAGES, Scripted


def settings(tmp_path) -> job.Settings:
    return job.Settings(
        storage=tmp_path / "storage",
        searxng_url="http://searx/search",
        api="http://api",
        env_file=str(tmp_path / ".env"),
        runlogs=tmp_path / "logs" / "deep-research",
    )


def builder(tmp_path, site="research"):
    source = tmp_path / "sites"
    (source / site).mkdir(parents=True)
    (source / site / "zola.toml").write_text("")
    content = tmp_path / "content"
    content.mkdir()
    return SimpleNamespace(
        source=source,
        content=content,
        output=tmp_path / "site",
        build=lambda name: None,
    )


def search(q):
    return [
        {"title": "Alpha news", "url": "https://a.example/", "snippet": q},
        {"title": "Beta about", "url": "https://b.example/", "snippet": q},
    ]


def the_line(s: job.Settings) -> dict:
    [file] = s.runlogs.glob("*.jsonl")
    [line] = [json.loads(x) for x in file.read_text().splitlines()]
    return line


@pytest.fixture
def published(monkeypatch):
    entries = []

    def fake_write(store, site, section, slug, title, date, extra, body):
        entries.append(
            {
                "site": site,
                "section": section,
                "title": title,
                "extra": extra,
                "body": body,
            }
        )
        return Entry(
            section,
            "alpha-and-beta",
            title,
            date,
            "https://h/research/reports/alpha-and-beta/",
        )

    monkeypatch.setattr(job.SiteStore, "write", fake_write)
    return entries


def test_a_run_that_fails_before_it_starts_is_logged(tmp_path):
    s = settings(tmp_path)
    result = job.run(
        job.Request("Why is the sky blue?", "quick"),
        s,
        lambda m: None,
        builder=Builder(
            source=tmp_path / "no-sites",
            themes=tmp_path / "themes",
            content=tmp_path / "zola",
            output=tmp_path / "site",
            zola="zola",
        ),
    )
    assert result["status"] == "failed"
    assert "failed: no Zola site named 'research'" in result["reply"]
    line = the_line(s)
    assert (line["status"], line["question"]) == ("failed", "Why is the sky blue?")
    assert "no Zola site named 'research'" in line["error"]
    assert line["models"] == {"planner": "glm-5.3", "worker": "deepseek-flash"}
    assert list((s.runlogs / "running").iterdir()) == []  # its marker is gone


def test_a_finished_run_is_published_embedded_logged_and_told(
    tmp_path, published, monkeypatch
):
    s = settings(tmp_path)
    embedded = []
    monkeypatch.setattr(
        publish,
        "embed_report",
        lambda *a: embedded.append(a) or "deep-research/alpha-x.json",
    )
    progress = []
    req = job.Request(
        "Tell me about Alpha and Beta",
        "quick",
        workspace="career",
        workspace_name="Career",
    )
    result = job.run(
        req,
        s,
        progress.append,
        builder=builder(tmp_path),
        llm=LLM(Scripted()),
        search=search,
        read=PAGES.get,
    )
    assert result["status"] == "ok"
    reply = result["reply"]
    assert reply.startswith(
        'Research report published: "Alpha and Beta"\n\nLink: https://h/research/reports/alpha-and-beta/'
    )
    assert re.search(
        r"\n\nCard: \[!\[Alpha and Beta\]\(https://h/_cards/\w+\.png\?v=\w+\)\]"
        r"\(https://h/research/reports/alpha-and-beta/\)\n\n",
        reply,
    )
    assert "Saved as research/alpha-and-beta.md in the agent's files." in reply
    assert "Added to this workspace's documents" in reply
    assert (
        "- Alpha earned EUR 12M in 2025." in reply
        and "[1]" not in reply.split("Key findings:")[1]
    )
    assert sorted(x["url"] for x in result["sources"]) == [
        "https://a.example/",
        "https://b.example/",
    ]
    [entry] = published
    assert (entry["site"], entry["section"], entry["extra"]["depth"]) == (
        "research",
        "reports",
        "quick",
    )
    assert embedded[0][0] == "career" and embedded[0][4] == "Alpha and Beta"
    assert (
        "Published at https://h/research/reports/alpha-and-beta/"
        in (s.reports_dir / "alpha-and-beta.md").read_text()
    )
    assert 'adding the report to workspace "Career"' in progress
    line = the_line(s)
    assert (line["status"], line["url"], line["published"], line["document"]) == (
        "ok",
        "https://h/research/reports/alpha-and-beta/",
        True,
        "deep-research/alpha-x.json",
    )
    assert line["stats"]["findings"] == 2 and "chat_closed" not in line
    assert any(e[1].startswith("Planning quick research") for e in line["events"])


def test_a_run_nobody_followed_to_the_end_notes_the_chat_closed(tmp_path, published):
    s = settings(tmp_path)
    req = job.Request("q", "quick", embed=False, workspace="career")
    result = job.run(
        req,
        s,
        lambda m: None,
        chat_closed=lambda: True,
        builder=builder(tmp_path),
        llm=LLM(Scripted()),
        search=search,
        read=PAGES.get,
    )
    assert result["status"] == "ok" and "document" not in the_line(s)
    assert the_line(s)["chat_closed"] is True


def test_a_report_the_site_couldnt_take_is_kept_in_the_agents_files(
    tmp_path, monkeypatch
):
    def fail(*a):
        raise RuntimeError("not saved: the site didn't build: zola exploded")

    monkeypatch.setattr(job.SiteStore, "write", fail)
    s = settings(tmp_path)
    result = job.run(
        job.Request("q", "quick"),
        s,
        lambda m: None,
        builder=builder(tmp_path),
        llm=LLM(Scripted()),
        search=search,
        read=PAGES.get,
    )
    assert result["status"] == "ok"
    assert (
        "couldn't be published to the research site: not saved: the site didn't build: zola exploded"
        in result["reply"]
    )
    assert "saved as research/alpha-and-beta.md" in result["reply"]
    line = the_line(s)
    assert (line["published"], line["file"]) == (
        False,
        str(s.reports_dir / "alpha-and-beta.md"),
    )


def test_requests_take_defaults_for_what_isnt_given():
    req = job.Request.of(
        "  q  ", depth=None, planner="", worker="w", embed=False, workspace=None
    )
    assert (
        req.question,
        req.depth,
        req.planner,
        req.worker,
        req.embed,
        req.workspace,
    ) == ("q", None, "glm-5.3", "w", False, None)


def test_the_planner_falls_back_only_from_glm_and_only_when_asked():
    assert job.Request("q").fallback == {"glm-5.3": "deepseek-flash"}
    assert job.Request("q", planner_fallback="off").fallback == {}
    assert job.Request("q", planner="deepseek-v4-pro").fallback == {}


def test_a_missing_key_fails_the_run_before_any_call(tmp_path):
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=ds\n")
    s = settings(tmp_path)
    result = job.run(
        job.Request("q", "quick"), s, lambda m: None, builder=builder(tmp_path)
    )
    assert "failed:" in result["reply"] and "_API_KEY is not set" in result["reply"]
