import re

from research.publish import (
    free_file_slug,
    report_file,
    save_report_file,
    save_then_publish,
)
from sites.store import Entry

TEXT = report_file(
    "Heat pumps",
    "2026-10-03",
    "How do heat pumps do in winter?",
    "https://example.org/reports/heat-pumps/",
    "## Summary\n\nThey work [1].\n\n## Sources\n\n1. x\n",
)


def test_report_file_heads_the_report_with_its_title_question_and_link():
    assert TEXT.startswith(
        "# Heat pumps\n\n_2026-10-03 · deep research on: How do heat pumps do in winter?_\n"
    )
    assert "Published at https://example.org/reports/heat-pumps/" in TEXT
    assert TEXT.endswith("1. x\n")


def test_save_report_file_writes_slug_md_creating_the_folder_and_replacing_an_older_copy(
    tmp_path,
):
    dir = tmp_path / "research"
    save_report_file(dir, "heat-pumps", "old")
    file = save_report_file(dir, "heat-pumps", TEXT)
    assert file == dir / "heat-pumps.md" and file.read_text() == TEXT


def test_free_file_slug_skips_taken_names(tmp_path):
    dir = tmp_path / "research"
    assert free_file_slug(dir, "!!") == "entry"
    assert (
        free_file_slug(dir, "Heat pumps: Åre, Malmö & Göteborg — a comparison")
        == "heat-pumps-are-malmo-goteborg-a-comparison"
    )
    save_report_file(dir, "heat-pumps", "x")
    save_report_file(dir, "heat-pumps-2", "x")
    assert free_file_slug(dir, "Heat pumps") == "heat-pumps-3"


def file_text(url):
    return report_file("Heat pumps", "2026-10-03", "q", url, "the report")


def test_save_then_publish_saves_the_file_before_publishing_then_adds_the_link(
    tmp_path,
):
    dir = tmp_path / "research"
    seen = []

    def publish():
        seen.append((dir / "heat-pumps.md").read_text())
        return Entry(
            "reports",
            "heat-pumps",
            "Heat pumps",
            "2026-10-03",
            "https://h/research/reports/heat-pumps/",
        )

    out = save_then_publish(dir, "Heat pumps", file_text, publish)
    assert "Not published on the research site." in seen[0]
    assert (
        out["file"] == str(dir / "heat-pumps.md") and out["build"].slug == "heat-pumps"
    )
    assert (
        "Published at https://h/research/reports/heat-pumps/"
        in (dir / "heat-pumps.md").read_text()
    )


def test_save_then_publish_keeps_the_file_when_publishing_fails(tmp_path):
    dir = tmp_path / "research"

    def fail():
        raise RuntimeError("not saved: the site didn't build: zola exploded")

    out = save_then_publish(dir, "T", file_text, fail)
    assert "build" not in out and "zola exploded" in out["publish_error"]
    assert re.search(r"Not published[\s\S]*the report", (dir / "t.md").read_text())
    # And when the file can't be written either, both errors are reported.
    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    both = save_then_publish(blocked, "T", file_text, fail)
    assert both.get("file_error") and both["publish_error"] and "file" not in both

