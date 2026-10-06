import pytest
from audit.checks import Env
from test_checks import API, NOW, SEARX


@pytest.fixture
def env(tmp_path):
    (tmp_path / "journal").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "content").mkdir()
    (tmp_path / "sites").mkdir()
    pages: dict[str, tuple[int, bytes]] = {}
    e = Env(
        journal_dir=tmp_path / "journal",
        api=API,
        searxng_url=SEARX,
        sites_source=tmp_path / "sites",
        sites_content=tmp_path / "content",
        runlogs=tmp_path / "logs",
        now=lambda: NOW,
        http=lambda url: pages.get(url, (404, b"not found")),
        run=lambda args: [],
        storage=tmp_path / "storage",
        pings=lambda storage: {},
    )
    e.pages = pages  # ty: ignore[unresolved-attribute] - the fake web, keyed by URL
    return e
