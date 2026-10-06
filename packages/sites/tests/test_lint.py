from pathlib import Path

import pytest
from sites import lint

REPO_ZOLA = Path(__file__).resolve().parents[1] / "zola"


def test_every_repo_template_passes_the_checks():
    templates = list(REPO_ZOLA.glob("**/templates/**/*.html"))
    assert len(templates) > 5
    for t in templates:
        assert lint.problems(t.read_text()) == [], t


@pytest.mark.parametrize(
    "snippet,match",
    [
        ('{{ load_data(path="/etc/passwd") }}', "load_data"),
        ('{{ get_env(name="HOME") }}', "get_env"),
        ("<script>alert(1)</script>", "<script>"),
        ('<form action="https://x.example">', "<form>"),
        ('<IFRAME src="/x">', "<iframe>"),
        ('<base href="https://x.example/">', "<base>"),
        ('<meta http-equiv="refresh" content="0;url=https://x.example">', "http-equiv"),
        ('<a href="/" onclick="x()">', "event handlers"),
        ('<a href="javascript:x()">', "javascript"),
        ('<p style="color:red">', "style="),
        ("{{ page.extra.summary | safe }}", "safe"),
        ("{% set x = page.title | safe %}{{ page.content | safe }}", "safe"),
        ("{% filter safe %}{{ page.title }}{% endfilter %}", "filter"),
    ],
)
def test_the_checks_refuse(snippet, match):
    assert any(match in p for p in lint.problems(snippet))
