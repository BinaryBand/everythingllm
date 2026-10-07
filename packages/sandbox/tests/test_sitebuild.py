import pytest
from sandbox.sitebuild import BuildError, assemble, theme_source


@pytest.fixture
def roots(tmp_path):
    system = tmp_path / "system"
    (system / "agent-site").mkdir(parents=True)
    (system / "agent-site" / "theme.toml").write_text('name = "agent-site"\n')
    shared = tmp_path / "shared"
    (shared / "career" / "themes" / "minimal").mkdir(parents=True)
    (shared / "career" / "themes" / "minimal" / "theme.toml").write_text("")
    return system, shared


def test_a_theme_comes_from_the_repo_or_a_workspace(roots):
    system, shared = roots
    conf = {"theme": "agent-site", "extra": {"build": {"theme_from": "system"}}}
    assert theme_source(conf, system, shared) == system / "agent-site"
    conf = {"theme": "minimal", "extra": {"build": {"theme_from": "career"}}}
    assert (
        theme_source(conf, system, shared) == shared / "career" / "themes" / "minimal"
    )
    assert (
        theme_source({"theme": "own"}, system, shared) is None
    )  # the site's own themes/


@pytest.mark.parametrize(
    ("conf", "why"),
    [
        (
            {"theme": "nope", "extra": {"build": {"theme_from": "system"}}},
            "no theme 'nope'",
        ),
        (
            {"theme": "../x", "extra": {"build": {"theme_from": "system"}}},
            "folder name",
        ),
        ({"extra": {"build": {"theme_from": "system"}}}, "folder name"),
        (
            {"theme": "minimal", "extra": {"build": {"theme_from": "../career"}}},
            "theme_from",
        ),
        ({"theme": "minimal", "extra": {"build": {"theme_from": "home"}}}, "no theme"),
    ],
)
def test_a_theme_that_isnt_there_or_isnt_a_name_is_refused(roots, conf, why):
    with pytest.raises(BuildError, match=why):
        theme_source(conf, *roots)


def test_assemble_copies_the_site_with_its_theme_but_not_git_or_old_output(
    tmp_path, roots
):
    site = tmp_path / "site"
    (site / "content").mkdir(parents=True)
    (site / "zola.toml").write_text(
        'theme = "agent-site"\n[extra.build]\ntheme_from = "system"\n'
    )
    (site / ".git").mkdir()
    (site / "public").mkdir()
    (site / "themes" / "agent-site").mkdir(parents=True)
    (site / "themes" / "agent-site" / "stale.txt").write_text("old copy")
    work = tmp_path / "work"
    assemble(site, work, *roots)
    assert sorted(p.name for p in work.iterdir()) == ["content", "themes", "zola.toml"]
    assert sorted(p.name for p in (work / "themes" / "agent-site").iterdir()) == [
        "theme.toml"
    ]
    with pytest.raises(BuildError, match="no zola.toml"):
        assemble(tmp_path / "content-only", tmp_path / "w2", *roots)
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "zola.toml").write_text("theme = ")
    with pytest.raises(BuildError, match="doesn't parse"):
        assemble(tmp_path / "bad", tmp_path / "w3", *roots)


def test_a_site_builds_with_the_repos_theme(tmp_path, sandbox_zola):
    """sitebuild.py for real, in the sandbox image (the only zola there is)."""
    site = tmp_path / "site"
    (site / "content").mkdir(parents=True)
    (site / "zola.toml").write_text(
        'base_url = "https://x.example/s"\ntitle = "S"\ntheme = "agent-site"\n'
        "compile_sass = false\nbuild_search_index = false\n"
        '[extra.build]\ntheme_from = "system"\n'
    )
    (site / "content" / "_index.md").write_text('+++\ntitle = "S"\n+++\n')
    out = tmp_path / "out"
    done = sandbox_zola(site, out, "https://pages.example/s")
    assert done.returncode == 0, done.stderr
    assert "https://pages.example/s" in (out / "index.html").read_text()


def test_a_system_sites_entries_join_its_content_but_not_their_index_or_dotfiles(
    tmp_path, roots
):
    site = tmp_path / "site"
    (site / "content" / "reports").mkdir(parents=True)
    (site / "content" / "reports" / "_index.md").write_text("from the repo")
    (site / "zola.toml").write_text(
        'theme = "agent-site"\n[extra.build]\ntheme_from = "system"\n'
    )
    entries = tmp_path / "entries"
    (entries / "reports").mkdir(parents=True)
    (entries / "reports" / "2026-10-06.md").write_text("a report")
    (entries / "reports" / "_index.md").write_text("never this one")
    (entries / ".build.lock").write_text("")
    work = tmp_path / "work"
    assemble(site, work, *roots, entries=entries)
    assert (work / "content" / "reports" / "_index.md").read_text() == "from the repo"
    assert (work / "content" / "reports" / "2026-10-06.md").read_text() == "a report"
    assert not (work / "content" / ".build.lock").exists()


def test_a_workspaces_theme_comes_without_its_symlinks(tmp_path, roots):
    """Another workspace's theme runs in this one's build, where its /project is mounted:
    zola copies static files through symlinks, so none come along."""
    system, shared = roots
    theme = shared / "career" / "themes" / "minimal"
    (theme / "static").mkdir()
    (theme / "static" / "style.css").write_text("body{}")
    private = tmp_path / "project"
    private.mkdir()
    (private / "secret.txt").write_text("private")
    (theme / "static" / "leak").symlink_to(private)
    (theme / "static" / "leak.txt").symlink_to(private / "secret.txt")
    site = tmp_path / "site"
    site.mkdir()
    (site / "zola.toml").write_text(
        'theme = "minimal"\n[extra.build]\ntheme_from = "career"\n'
    )
    work = tmp_path / "work"
    assemble(site, work, system, shared)
    copied = work / "themes" / "minimal" / "static"
    assert sorted(p.name for p in copied.iterdir()) == ["style.css"]


def test_a_workspaces_theme_reached_through_a_symlink_is_refused(tmp_path, roots):
    system, shared = roots
    elsewhere = tmp_path / "project" / "minimal2"
    elsewhere.mkdir(parents=True)
    (elsewhere / "theme.toml").write_text("")
    (shared / "career" / "themes" / "minimal2").symlink_to(elsewhere)
    site = tmp_path / "site"
    site.mkdir()
    (site / "zola.toml").write_text(
        'theme = "minimal2"\n[extra.build]\ntheme_from = "career"\n'
    )
    with pytest.raises(BuildError, match="is a symlink"):
        assemble(site, tmp_path / "work", system, shared)


def test_a_workspaces_theme_swapped_for_a_symlink_after_the_check_isnt_followed(
    tmp_path, roots, monkeypatch
):
    """The workspace the theme is from can write its /shared while this build runs: a
    folder swapped for a symlink between theme_source's check and the copy is refused."""
    import sandbox.sitebuild as sitebuild

    system, shared = roots
    private = tmp_path / "project"
    private.mkdir()
    (private / "secret.txt").write_text("private")
    site = tmp_path / "site"
    site.mkdir()
    (site / "zola.toml").write_text(
        'theme = "minimal"\n[extra.build]\ntheme_from = "career"\n'
    )
    themes = shared / "career" / "themes"
    checked = sitebuild.theme_source

    def then_swap(*args):
        found = checked(*args)
        (themes / "minimal").rename(themes / "old")
        (themes / "minimal").symlink_to(private)
        return found

    monkeypatch.setattr(sitebuild, "theme_source", then_swap)
    with pytest.raises(BuildError, match="is a symlink"):
        assemble(site, tmp_path / "work", system, shared)
    assert not (tmp_path / "work" / "themes" / "minimal" / "secret.txt").exists()
