import http.server
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import hostrpc
import pytest
import tomllib
from sites import build
from sites.build import BUILD_SECONDS, MARKER, Builder, BuildError, sandboxed
from sites.store import SiteError, SiteStore, _split

REPO_ZOLA = Path(__file__).resolve().parents[1] / "zola"

CONFIG = """base_url = "https://pages.example/news"
title = "Daily News"
[extra]
agent_help = "One entry per day."
"""


@pytest.fixture
def source(tmp_path):
    site = tmp_path / "src" / "news"
    (site / "content" / "editions").mkdir(parents=True)
    (site / "content" / "editions" / "_index.md").write_text("+++\n+++\n")
    (site / "zola.toml").write_text(CONFIG)
    return tmp_path / "src"


@pytest.fixture
def store(source, tmp_path):
    (tmp_path / "content").mkdir()
    return SiteStore(source, tmp_path / "content")


def test_sites_describe_sections_and_help(store):
    [site] = store.sites()
    assert (site.name, site.title, site.url) == (
        "news",
        "Daily News",
        "https://pages.example/news/",
    )
    assert site.sections == ["editions"]
    assert site.help == "One entry per day."


def test_agent_can_read_but_not_change_a_readonly_section(source, tmp_path):
    (source / "news" / "content" / "articles").mkdir()
    (source / "news" / "content" / "articles" / "_index.md").write_text(
        '+++\ntitle = "Articles"\n\n[extra]\nagent_readonly = true\n+++\n'
    )
    (tmp_path / "content").mkdir()
    tooling = SiteStore(source, tmp_path / "content")
    tooling.write("news", "articles", "us-1", "By the writer", "2026-10-03")
    agent = SiteStore(source, tmp_path / "content", agent=True)
    assert agent.site("news").readonly == ["articles"]
    assert agent.get("news", "articles", "us-1")[0].title == "By the writer"
    with pytest.raises(SiteError, match="can read it but not change it"):
        agent.write(
            "news", "articles", "us-1", "By the agent", "2026-10-03", overwrite=True
        )
    with pytest.raises(SiteError, match="can read it but not change it"):
        agent.delete("news", "articles", "us-1")
    agent.write("news", "editions", "2026-10-03", "Edition", "2026-10-03")


def test_write_then_read(store, tmp_path):
    extra = {"sections": [{"name": "US", "stories": [{"headline": "Å & <b>"}]}]}
    entry = store.write(
        "news", "editions", "2026-10-03", "Daily  News — x", "2026-10-03", extra
    )
    assert entry.url == "https://pages.example/news/editions/2026-10-03/"
    assert entry.title == "Daily News — x"
    meta, body = _split((tmp_path / "content/news/editions/2026-10-03.md").read_text())
    assert meta["extra"] == extra and body == "\n"
    _, got, _ = store.get("news", "editions", "2026-10-03")
    assert got == extra
    assert [e.slug for e in store.entries("news")] == ["2026-10-03"]


def test_write_and_delete_rebuild_the_site(store):
    built = []
    store.build = built.append
    store.write("news", "editions", "a", "A", "2026-10-03")
    store.delete("news", "editions", "a")
    assert built == ["news", "news"]


def test_overwrite_required(store):
    store.write("news", "editions", "a", "A", "2026-10-03")
    with pytest.raises(SiteError, match="overwrite=true"):
        store.write("news", "editions", "a", "A", "2026-10-03")
    store.write("news", "editions", "a", "B", "2026-10-03", overwrite=True)
    assert store.entries("news")[0].title == "B"


def test_body_html_is_escaped(store, tmp_path):
    store.write(
        "news", "editions", "a", "A", "2026-10-03", body="<script>x</script> *ok*"
    )
    text = (tmp_path / "content/news/editions/a.md").read_text()
    assert "<script>" not in text and "&lt;script>" in text


@pytest.mark.parametrize(
    "args",
    [
        ("nope", "editions", "a", "A", "2026-10-03"),
        ("news", "drafts", "a", "A", "2026-10-03"),
        ("news", "editions", "../a", "A", "2026-10-03"),
        ("news", "editions", "_index", "A", "2026-10-03"),
        ("news", "editions", "a", " ", "2026-10-03"),
        ("news", "editions", "a", "A", "Oct 3"),
    ],
)
def test_bad_writes_rejected(store, args):
    with pytest.raises(SiteError):
        store.write(*args)


def test_delete(store):
    store.write("news", "editions", "a", "A", "2026-10-03")
    store.delete("news", "editions", "a")
    assert store.entries("news") == []
    with pytest.raises(SiteError):
        store.delete("news", "editions", "a")


def builder(tmp_path, zola="zola"):
    (tmp_path / "content").mkdir(exist_ok=True)
    (tmp_path / "site").mkdir(exist_ok=True)
    return Builder(
        REPO_ZOLA / "sites",
        REPO_ZOLA / "themes",
        tmp_path / "content",
        tmp_path / "site",
        zola,
    )


@pytest.fixture
def repo_store(tmp_path):
    """The repo's real sites, writing and building into tmp_path."""
    b = builder(tmp_path)
    return SiteStore(REPO_ZOLA / "sites", tmp_path / "content", build=b.build)


def test_build_never_replaces_a_published_page(tmp_path):
    b = builder(tmp_path)
    (tmp_path / "site" / "news").mkdir()
    (tmp_path / "site" / "news" / "index.html").write_text("someone's page")
    with pytest.raises(BuildError, match="wasn't built from sites/news"):
        b.build("news")
    assert (tmp_path / "site" / "news" / "index.html").read_text() == "someone's page"


def test_build_reports_a_missing_zola_or_site(tmp_path):
    with pytest.raises(BuildError, match="can't run zola"):
        builder(tmp_path, zola=str(tmp_path / "no-zola")).build("news")
    with pytest.raises(BuildError, match="no Zola site 'nope'"):
        builder(tmp_path).build("nope")


@pytest.mark.skipif(shutil.which("zola") is None, reason="zola not installed")
def test_news_site_builds_with_entries(repo_store, tmp_path):
    """The real news site and theme build through the store, with the home page showing the newest edition."""
    for day, headline in [("2026-10-02", "Older"), ("2026-10-03", "Newer & <i>")]:
        repo_store.write(
            "news",
            "editions",
            day,
            f"Daily News — {day}",
            day,
            {
                "sections": [
                    {
                        "name": "US",
                        "stories": [{"headline": headline, "url": "javascript:x"}],
                    },
                    {"name": "World", "stories": []},
                ]
            },
        )
    out = tmp_path / "site" / "news"
    assert (out / MARKER).exists()
    assert not list((tmp_path / "site").glob(".news.*")), (
        "temporary build directories left behind"
    )
    home = (out / "index.html").read_text()
    assert "Newer &amp; &lt;i&gt;" in home and "No items retrieved today." in home
    assert "javascript:" not in home
    assert "Daily News — 2026-10-02" in home  # under Earlier editions
    assert (out / "editions" / "2026-10-02" / "index.html").exists()
    assert "2026-10-03" in (out / "editions" / "index.html").read_text()
    assert "<style" not in home and "style=" not in home


def test_every_stylesheet_a_repo_site_lists_is_there():
    for config in (REPO_ZOLA / "sites").glob("*/zola.toml"):
        for sheet in tomllib.loads(config.read_text())["extra"].get("stylesheets", []):
            assert (config.parent / "static" / sheet).is_file(), sheet


def test_news_sections_carry_their_config(tmp_path):
    (tmp_path / "content").mkdir()
    store = SiteStore(REPO_ZOLA / "sites", tmp_path / "content")
    news = store.site("news")
    assert news.readonly == ["articles"]


def run_sites_write(monkeypatch, capsys, tmp_path, request, zola="zola"):
    """Run sites-write with the repo's sites and tmp_path storage; returns (exit code, output)."""
    import io

    from sites import write

    # Built here with the host's zola, as a Builder without the sandbox builds a site
    # whose theme is the repo's.
    monkeypatch.setattr(build, "sandbox_build", None)
    (tmp_path / "content").mkdir(exist_ok=True)
    (tmp_path / "site").mkdir(exist_ok=True)
    for name, value in {
        "SITES_SOURCE": REPO_ZOLA / "sites",
        "SITES_CONTENT": tmp_path / "content",
        "SITES_OUTPUT": tmp_path / "site",
        "ZOLA": zola,
    }.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request)))
    code = 0
    try:
        write.main()
    except SystemExit as e:
        code = e.code
    return code, json.loads(capsys.readouterr().out)


REPORT = {
    "site": "research",
    "section": "reports",
    "title": "Värme <pumpar>",
    "date": "2026-10-03",
    "extra": {"depth": "quick"},
    "body": "## Summary",
}


@pytest.mark.skipif(shutil.which("zola") is None, reason="zola not installed")
def test_sites_write_saves_builds_and_picks_a_free_slug(monkeypatch, capsys, tmp_path):
    code, out = run_sites_write(monkeypatch, capsys, tmp_path, REPORT)
    assert code == 0
    assert out == {
        "url": "http://127.0.0.1:8445/research/reports/varme-pumpar/",
        "slug": "varme-pumpar",
        "published": True,
        "error": None,
    }
    assert (
        tmp_path / "site" / "research" / "reports" / "varme-pumpar" / "index.html"
    ).exists()
    _, again = run_sites_write(monkeypatch, capsys, tmp_path, REPORT)
    assert again["slug"] == "varme-pumpar-2"


def test_sites_write_reports_a_failed_build_and_bad_requests(
    monkeypatch, capsys, tmp_path
):
    code, out = run_sites_write(
        monkeypatch, capsys, tmp_path, REPORT, zola=str(tmp_path / "no-zola")
    )
    assert code == 1 and out["error"].startswith(
        "not saved: the site didn't build: can't run zola"
    )
    assert not (
        tmp_path / "content" / "research" / "reports" / "varme-pumpar.md"
    ).exists()
    code, out = run_sites_write(
        monkeypatch, capsys, tmp_path, {**REPORT, "site": "nope"}
    )
    assert code == 1 and "no site named 'nope'" in out["error"]
    code, out = run_sites_write(monkeypatch, capsys, tmp_path, {"site": "research"})
    assert code == 1 and out["error"].startswith("bad request")


# --- Zola runs content as templates; bodies must stay text ----------------------------


def test_tera_syntax_in_bodies_is_broken_up(store, tmp_path):
    store.write(
        "news",
        "editions",
        "a",
        "A",
        "2026-10-03",
        body='{{ get_env(name="X") }} `${{ secrets.T }}` {% if x %} {# c #}',
    )
    text = (tmp_path / "content/news/editions/a.md").read_text()
    assert "{{" not in text and "{%" not in text and "{#" not in text
    assert '{\u200b{ get_env(name="X") }}' in text


@pytest.mark.skipif(shutil.which("zola") is None, reason="zola not installed")
def test_tera_syntax_in_a_body_builds_as_literal_text(
    repo_store, tmp_path, monkeypatch
):
    monkeypatch.setenv("X", "secret-value-123")
    repo_store.write(
        "research",
        "reports",
        "leak",
        "Leak",
        "2026-10-03",
        {"depth": "quick"},
        body='Env: {{ get_env(name="X") }}\n\nCI: `${{ secrets.T }}`\n\n'
        '{{ load_data(path="/etc/passwd") }}',
    )
    html = (
        tmp_path / "site" / "research" / "reports" / "leak" / "index.html"
    ).read_text()
    assert "secret-value-123" not in html and "root:" not in html
    shown = html.replace("\u200b", "")
    assert "get_env(name=&quot;X&quot;) }}" in shown or 'get_env(name="X") }}' in shown
    assert "${{ secrets.T }}" in shown


def fake_zola(cmd, **kw):
    """Stands in for subprocess.run of zola: makes an empty output directory."""
    Path(cmd[cmd.index("--output-dir") + 1]).mkdir()
    return subprocess.CompletedProcess(cmd, 0, "", "")


def test_zola_gets_no_environment_but_path(tmp_path, monkeypatch):
    """Even a template calling get_env finds nothing: zola runs with PATH and HOME only."""
    monkeypatch.setenv("X", "secret")
    seen = {}

    def run(cmd, **kw):
        seen.update(kw["env"])
        return fake_zola(cmd)

    monkeypatch.setattr("sites.build.subprocess.run", run)
    builder(tmp_path).build("news")
    assert set(seen) == {"PATH", "HOME"} and seen["HOME"] != str(Path.home())


# --- a change only stays when the site builds -----------------------------------------


def fail_build(name):
    raise BuildError("zola build failed for news:\nError: bad template")


def test_failed_build_removes_a_new_entry(store):
    store.build = fail_build
    with pytest.raises(
        SiteError, match="not saved: the site didn't build: zola build failed"
    ):
        store.write("news", "editions", "a", "A", "2026-10-03")
    assert store.entries("news") == []
    assert not list((store.content / "news" / "editions").iterdir()), (
        "temp files left behind"
    )


def test_failed_build_restores_an_overwritten_entry(store):
    store.write("news", "editions", "a", "Old", "2026-10-03", body="old body")
    file = store.content / "news" / "editions" / "a.md"
    os.utime(file, (1_700_000_000, 1_700_000_000))
    store.build = fail_build
    with pytest.raises(SiteError, match="not saved"):
        store.write(
            "news",
            "editions",
            "a",
            "New",
            "2026-10-03",
            body="new body",
            overwrite=True,
        )
    entry, _, body = store.get("news", "editions", "a")
    assert entry.title == "Old" and body.strip() == "old body"
    # The old mtime too, so the entry isn't newer than the last build.
    assert file.stat().st_mtime == 1_700_000_000


def test_failed_build_restores_a_deleted_entry(store):
    store.write("news", "editions", "a", "A", "2026-10-03")
    file = store.content / "news" / "editions" / "a.md"
    os.utime(file, (1_700_000_000, 1_700_000_000))
    store.build = fail_build
    with pytest.raises(SiteError, match="not saved"):
        store.delete("news", "editions", "a")
    assert [e.slug for e in store.entries("news")] == ["a"]
    assert file.stat().st_mtime == 1_700_000_000


@pytest.mark.parametrize(
    "date", ["20261001", "2026-W40-1", "2026-10-1", "2026-13-01", " 2026-10-01", None]
)
def test_bad_dates_rejected(store, date):
    with pytest.raises(SiteError, match="date must be YYYY-MM-DD"):
        store.write("news", "editions", "a", "A", date)
    assert store.entries("news") == []


def test_control_characters_are_escaped(store, tmp_path):
    store.write(
        "news",
        "editions",
        "a",
        "A B",
        "2026-10-03",
        {"note": "x\x00\x1b\x85y Å\u2028👋"},
    )
    head = (tmp_path / "content/news/editions/a.md").read_text().split("\n---\n")[0]
    assert all(e in head for e in ("\\u0000", "\\u001b", "\\u0085", "\\u2028"))
    assert (
        "Å" in head and "👋" in head
    )  # as they are: YAML has no escape for a surrogate pair
    _, extra, _ = store.get("news", "editions", "a")
    assert extra == {"note": "x\x00\x1b\x85y Å\u2028👋"}


@pytest.mark.skipif(shutil.which("zola") is None, reason="zola not installed")
def test_emoji_and_odd_characters_build(tmp_path):
    """A log line quoted in a report ("byeee!! 👋") once stopped a site building: Zola's
    YAML parser rejects JSON's surrogate-pair escapes."""
    b = builder(tmp_path)
    store = SiteStore(b.source, b.content, build=b.build)
    odd = "bye 👋 é \x00\x1b\x7f\x85\u2028\u2029\ufeff\uffff\udc4b"
    store.write(
        "research",
        "reports",
        "2026-10-05",
        "Notes 👋",
        "2026-10-05",
        {"depth": "quick", "question": odd},
    )
    _, extra, _ = store.get("research", "reports", "2026-10-05")
    assert extra["question"] == odd.replace("\udc4b", "\ufffd")
    assert "Notes 👋" in (tmp_path / "site" / "research" / "index.html").read_text()


@pytest.mark.skipif(shutil.which("zola") is None, reason="zola not installed")
def test_dated_slugs_keep_their_own_urls(repo_store, tmp_path):
    """Zola strips a leading date from file names unless the front matter names the slug."""
    a = repo_store.write(
        "research",
        "reports",
        "2026-10-01-notes",
        "Notes",
        "2026-10-01",
        {"depth": "quick"},
    )
    repo_store.write(
        "research",
        "reports",
        "2026-10-02-notes",
        "More notes",
        "2026-10-02",
        {"depth": "quick"},
    )
    assert a.url.endswith("/research/reports/2026-10-01-notes/")
    out = tmp_path / "site" / "research" / "reports"
    assert "Notes" in (out / "2026-10-01-notes" / "index.html").read_text()
    assert "More notes" in (out / "2026-10-02-notes" / "index.html").read_text()
    assert not (out / "notes").exists()
    assert [e.slug for e in repo_store.entries("research")] == [
        "2026-10-02-notes",
        "2026-10-01-notes",
    ]


@pytest.mark.skipif(shutil.which("zola") is None, reason="zola not installed")
def test_build_all_builds_the_rest_and_reports_every_failure(tmp_path):
    b = builder(tmp_path)
    (tmp_path / "site" / "news").mkdir()
    (tmp_path / "site" / "news" / "index.html").write_text("someone's page")
    with pytest.raises(BuildError) as e:
        b.build()
    assert "sites/news" in str(e.value)
    assert (tmp_path / "site" / "research" / MARKER).exists()


def test_failed_swap_puts_the_last_build_back(tmp_path, monkeypatch):
    b = builder(tmp_path)
    dest = tmp_path / "site" / "news"
    dest.mkdir()
    (dest / MARKER).write_text("")
    (dest / "index.html").write_text("last good build")
    real_rename = os.rename

    def rename(src, dst):
        if Path(src).name == ".news.new":
            raise OSError("disk trouble")
        real_rename(src, dst)

    monkeypatch.setattr("sites.build.subprocess.run", fake_zola)
    monkeypatch.setattr("sites.build.os.rename", rename)
    with pytest.raises(BuildError, match="disk trouble"):
        b.build("news")
    assert (dest / "index.html").read_text() == "last good build"
    assert not list((tmp_path / "site").glob(".news.*"))


# --- the public URL comes from host.env -----------------------------------------------


def test_site_urls_come_from_public_host_else_zola_toml(store, source, monkeypatch):
    from sites import store as store_mod

    assert store.site("news").url == "https://pages.example/news/"
    hosts = source.parent / "host.env"
    hosts.write_text(
        "# this machine\nANYTHINGLLM_STORAGE=/x\nPUBLIC_HOST=box.tail.ts.net\n"
    )
    monkeypatch.setattr(store_mod, "host_file", lambda src: hosts)
    assert store.site("news").url == "https://box.tail.ts.net:8445/news/"
    monkeypatch.setenv("PUBLIC_HOST", "other.ts.net")  # the environment's wins
    assert store.site("news").url == "https://other.ts.net:8445/news/"


def test_host_env_is_found_at_the_repo_root(monkeypatch):
    monkeypatch.undo()  # the conftest points host_file elsewhere
    from sites import store as store_mod

    assert store_mod.host_file(Path("/mcp/packages/sites/zola/sites")) == Path(
        "/mcp/host.env"
    )


def test_build_passes_the_public_url_to_zola(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        "sites.build.subprocess.run",
        lambda cmd, **kw: seen.append(cmd) or fake_zola(cmd),
    )
    builder(tmp_path).build("news")
    assert "--base-url" not in seen[-1]
    monkeypatch.setenv("PUBLIC_HOST", "box.tail.ts.net")
    builder(tmp_path).build("news")
    assert seen[-1][-2:] == ["--base-url", "https://box.tail.ts.net:8445/news"]


@pytest.mark.skipif(shutil.which("zola") is None, reason="zola not installed")
def test_built_links_use_the_public_host(repo_store, tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_HOST", "box.tail.ts.net")
    entry = repo_store.write("research", "reports", "heat", "Heat", "2026-10-03")
    assert entry.url == "https://box.tail.ts.net:8445/research/reports/heat/"
    home = (tmp_path / "site" / "research" / "index.html").read_text()
    assert 'href="https://box.tail.ts.net:8445/research/reports/heat/"' in home
    assert "127.0.0.1" not in home


def test_from_env_puts_entries_and_sites_on_the_host(monkeypatch):
    for var in ("SITES_SOURCE", "SITES_CONTENT", "SITES_OUTPUT", "ZOLA"):
        monkeypatch.delenv(var, raising=False)
    b = Builder.from_env()
    assert b.source == REPO_ZOLA / "sites"
    assert (b.content, b.output, b.zola) == (
        Path("~/.local/share/everythingllm/pages/entries").expanduser(),
        Path("~/.local/share/everythingllm/pages/public").expanduser(),
        "/usr/local/bin/zola",
    )
    monkeypatch.setenv("ANYTHINGLLM_STORAGE", "/data/allm")
    assert Builder.from_env().output == b.output  # storage doesn't move the site


# --- zola runs without a network, and not for long -------------------------------------


@pytest.mark.skipif(shutil.which("zola") is None, reason="zola not installed")
@pytest.mark.skipif(not sandboxed(), reason="no unprivileged user namespaces here")
def test_a_template_cant_fetch_anything_while_the_site_builds(tmp_path):
    """load_data takes URLs; even the host's own loopback is out of reach of a build."""
    asked = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            asked.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"secret")

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        site = tmp_path / "src" / "leak"
        (site / "templates").mkdir(parents=True)
        (site / "zola.toml").write_text('base_url = "https://pages.example/leak"\n')
        (site / "templates" / "index.html").write_text(
            f'{{{{ load_data(url="http://127.0.0.1:{server.server_port}/x", format="plain") }}}}'
        )
        (tmp_path / "themes").mkdir()
        b = Builder(
            tmp_path / "src",
            tmp_path / "themes",
            tmp_path / "content",
            tmp_path / "site",
            "zola",
        )
        (tmp_path / "content").mkdir()
        (tmp_path / "site").mkdir()
        with pytest.raises(BuildError, match="zola build failed for leak"):
            b.build("leak")
    finally:
        server.shutdown()
    assert asked == [] and not (tmp_path / "site" / "leak").exists()


def test_a_build_that_runs_too_long_is_stopped(tmp_path, monkeypatch):
    def run(cmd, **kw):
        assert kw["timeout"] == BUILD_SECONDS
        Path(cmd[cmd.index("--output-dir") + 1]).mkdir()
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr("sites.build.subprocess.run", run)
    with pytest.raises(BuildError, match=f"took longer than {BUILD_SECONDS} s"):
        builder(tmp_path).build("news")
    assert list((tmp_path / "site").iterdir()) == []


# --- sites whose theme comes from elsewhere are built in the sandbox ---


def theme_from_site(tmp_path, origin):
    """A copy of the repo's sites in which research takes its theme from `origin`."""
    source = tmp_path / "src"
    shutil.copytree(REPO_ZOLA / "sites", source)
    toml = source / "research" / "zola.toml"
    text = toml.read_text()
    assert 'theme_from = "system"' in text
    toml.write_text(text.replace('theme_from = "system"', f'theme_from = "{origin}"'))
    (tmp_path / "content").mkdir(exist_ok=True)
    (tmp_path / "site").mkdir(exist_ok=True)
    return source


def test_a_theme_from_site_is_built_by_the_sandbox_and_swapped_in_here(tmp_path):
    source = theme_from_site(tmp_path, "education")
    asked = []

    def remote(name):
        asked.append(name)
        new = tmp_path / "site" / f".{name}.new"
        new.mkdir()
        (new / "index.html").write_text("built in the sandbox")
        return new

    b = Builder(
        source,
        REPO_ZOLA / "themes",
        tmp_path / "content",
        tmp_path / "site",
        "no-zola-needed",
        remote=remote,
    )
    assert (b.theme_from("research"), b.theme_from("news")) == ("education", "system")
    [dest] = b.build("research")
    assert asked == ["research"]
    assert (dest / "index.html").read_text() == "built in the sandbox"
    assert (dest / ".zola-site").exists()  # marked and swapped like any build
    assert not (tmp_path / "site" / ".research.new").exists()


def test_marking_a_build_follows_no_symlink(tmp_path):
    """The pages site is writable by the service containers, which could swap a symlink
    in for the build or its marker before a host process marks it."""
    source = theme_from_site(tmp_path, "system")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("host file")

    def as_link(name):
        new = tmp_path / "site" / f".{name}.new"
        new.symlink_to(outside)
        return new

    def marker_as_link(name):
        new = tmp_path / "site" / f".{name}.new"
        new.mkdir()
        (new / MARKER).symlink_to(outside / "keep")
        return new

    for remote in (as_link, marker_as_link):
        b = Builder(
            source,
            REPO_ZOLA / "themes",
            tmp_path / "content",
            tmp_path / "site",
            "zola",
            remote=remote,
        )
        with pytest.raises(BuildError, match="couldn't mark"):
            b.build("research")
        assert sorted(p.name for p in outside.iterdir()) == ["keep"]
        assert (outside / "keep").read_text() == "host file"
        assert not (tmp_path / "site" / "research").exists()
        shutil.rmtree(tmp_path / "site" / ".research.new", ignore_errors=True)
        (tmp_path / "site" / ".research.new").unlink(missing_ok=True)


def test_a_sandbox_build_that_fails_or_lands_elsewhere_changes_nothing(tmp_path):
    source = theme_from_site(tmp_path, "system")
    live = tmp_path / "site" / "research"
    live.mkdir()
    (live / ".zola-site").touch()
    (live / "index.html").write_text("the last good build")

    def failing(name):
        raise BuildError(f"zola build failed for {name} (in the sandbox): boom")

    def elsewhere(name):
        other = tmp_path / "elsewhere"
        other.mkdir()
        return other

    for remote, why in ((failing, "boom"), (elsewhere, "SANDBOX_SITE_DIR differ")):
        b = Builder(
            source,
            REPO_ZOLA / "themes",
            tmp_path / "content",
            tmp_path / "site",
            "zola",
            remote=remote,
        )
        with pytest.raises(BuildError, match=why):
            b.build("research")
        assert (live / "index.html").read_text() == "the last good build"
    assert not (tmp_path / "elsewhere").exists()


def test_without_the_sandbox_only_a_repo_theme_builds_here(tmp_path):
    source = theme_from_site(tmp_path, "education")
    b = Builder(
        source, REPO_ZOLA / "themes", tmp_path / "content", tmp_path / "site", "zola"
    )
    with pytest.raises(BuildError, match="only the sandbox may build it"):
        b.build("research")
    assert not (tmp_path / "site" / "research").exists()


def test_from_env_builds_theme_from_sites_in_the_sandbox(monkeypatch):
    b = build.Builder.from_env()
    assert b.remote is build.sandbox_build and not b.sandbox_only
    monkeypatch.setenv("SITES_SANDBOX_ONLY", "1")  # as in sites-runner's container
    assert build.Builder.from_env().sandbox_only


def test_sandbox_only_refuses_a_site_the_sandbox_wont_build(tmp_path, monkeypatch):
    """sites-runner's container has no zola and no unshare: a site without theme_from
    fails with a reason, rather than running zola there (or without its namespace)."""
    source = theme_from_site(tmp_path, "system")
    toml = source / "research" / "zola.toml"
    toml.write_text(toml.read_text().replace('theme_from = "system"', ""))

    def no_zola(*a, **kw):
        raise AssertionError("zola ran")

    monkeypatch.setattr("sites.build.subprocess.run", no_zola)
    monkeypatch.setattr("sites.build.subprocess.call", no_zola)
    asked = []

    def remote(name):
        asked.append(name)
        new = tmp_path / "site" / f".{name}.new"
        new.mkdir()
        return new

    b = Builder(
        source,
        REPO_ZOLA / "themes",
        tmp_path / "content",
        tmp_path / "site",
        "zola",
        remote=remote,
        sandbox_only=True,
    )
    with pytest.raises(BuildError, match=r"research can't be built here.*theme_from"):
        b.build("research")
    assert not (tmp_path / "site" / "research").exists()
    [dest] = b.build("news")  # theme_from = "system": the sandbox's, as ever
    assert asked == ["news"] and (dest / ".zola-site").exists()


def test_every_repo_site_is_built_in_the_sandbox():
    """sites-runner runs in a container without zola (SITES_SANDBOX_ONLY), so a repo site
    without [extra.build] theme_from couldn't be written to. Add theme_from = "system"."""
    b = Builder.from_env()
    assert b.site_names()
    for name in b.site_names():
        assert b.theme_from(name), f"{name}'s zola.toml names no theme_from"


def test_a_sandbox_runner_without_its_build_socket_builds_through_its_own(
    tmp_path, monkeypatch
):
    """Deploy rebuilds the sites before anyone restarts sandbox-runner onto the new code,
    which is what makes sandbox-build/runner.sock: until then the host builds through the
    runner's own socket, as it did before."""
    monkeypatch.delenv("SANDBOX_BUILD_SOCKET", raising=False)
    monkeypatch.delenv("SANDBOX_SOCKET", raising=False)
    monkeypatch.setenv("ANYTHINGLLM_STORAGE", str(tmp_path))
    socks = tmp_path / "everythingllm"
    new, old = socks / "sandbox-build" / "runner.sock", socks / "sandbox" / "runner.sock"
    old.parent.mkdir(parents=True)
    old.touch()
    assert build.build_socket() == old
    new.parent.mkdir(parents=True)
    new.touch()
    assert build.build_socket() == new
    # Named, or with neither there (a container), it's the build socket, as named.
    new.unlink()
    old.unlink()
    assert build.build_socket() == new
    monkeypatch.setenv("SANDBOX_BUILD_SOCKET", str(tmp_path / "x.sock"))
    old.touch()
    assert build.build_socket() == tmp_path / "x.sock"


def test_the_sandbox_build_turns_runner_errors_into_build_errors(monkeypatch):
    def refuse(*a, **kw):
        raise hostrpc.RunnerError("The sandbox runner isn't running on the host")

    monkeypatch.setattr(build.hostrpc, "request_sync", refuse)
    with pytest.raises(
        BuildError,
        match="research \\(in the sandbox\\): The sandbox runner isn't running",
    ):
        build.sandbox_build("research")


def test_the_build_lock_never_empties_a_file_through_a_symlink(tmp_path):
    """The sites container can write pages/entries: its .build.lock made a symlink to a
    host file mustn't have a build on the host truncate that file."""
    b = builder(tmp_path)
    target = tmp_path / "authorized_keys"
    target.write_text("ssh-ed25519 AAAA me")
    (tmp_path / "content" / ".build.lock").symlink_to(target)
    with pytest.raises(OSError):
        b.build("news")
    assert target.read_text() == "ssh-ed25519 AAAA me"
