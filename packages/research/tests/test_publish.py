from research.publish import report_file, save_report, slugify

TEXT = report_file(
    "Heat pumps",
    "2026-10-03",
    "How do heat pumps do in winter?",
    "## Summary\n\nThey work [1].\n\n## Sources\n\n1. x\n",
)


def test_report_file_heads_the_report_with_its_title_and_question():
    assert TEXT.startswith(
        "# Heat pumps\n\n_2026-10-03 · deep research on: How do heat pumps do in winter?_\n"
    )
    assert TEXT.endswith("1. x\n")


def test_a_reports_slug_is_its_title_in_ascii():
    assert slugify("!!") == ""
    assert (
        slugify("Heat pumps: Åre, Malmö & Göteborg — a comparison")
        == "heat-pumps-are-malmo-goteborg-a-comparison"
    )


def test_save_report_makes_the_folder_and_never_replaces_another_report(tmp_path):
    dir = tmp_path / "research"
    first = save_report(dir, "Heat pumps", "old")
    second = save_report(dir, "Heat pumps", TEXT)
    assert (first.name, second.name) == ("heat-pumps.md", "heat-pumps-2.md")
    assert first.read_text() == "old" and second.read_text() == TEXT
    assert save_report(dir, "!!", "x").name == "report.md"
