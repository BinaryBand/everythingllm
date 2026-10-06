import json
import re
from typing import cast

import pytest
from sites import cards, tools
from sites.store import Entry, Site, SiteStore


@pytest.mark.parametrize(
    ("extra", "body", "description"),
    [
        ({"summary": "All green.", "question": "q"}, "Body.", "All green."),
        ({"question": "Why is the sky blue?"}, "Body.", "Why is the sky blue?"),
        (
            {"summary": " "},
            "# Head\n\n- a list\n\nThe **first** [real](https://x) one[^1].",
            "The first real one.",
        ),
        ({}, "| a | b |\n|---|---|\n\n```\ncode\n```", ""),
    ],
)
def test_describe(extra, body, description):
    assert cards.describe(extra, body) == description


def test_an_entry_card_uses_the_site_title_or_its_name(tmp_path):
    class Store:
        def site(self, name):
            raise KeyError("base_url")

    entry = Entry("daily", "x", "Today", "2026-10-06", "https://h/news/daily/x/")
    line = cards.entry_card(
        tmp_path, cast(SiteStore, Store()), "news", entry, {}, "Hello."
    )
    assert re.fullmatch(
        r"\[!\[Today\]\(https://h/_cards/\w+\.png\?v=\w+\)\]\(https://h/news/daily/x/\)",
        line,
    )


def test_a_site_card_links_its_home_page(tmp_path):
    site = Site("news", "Daily News", "Headlines, daily.", "https://h/news/", [], "")
    line = cards.site_card(tmp_path, site)
    assert re.fullmatch(
        r"\[!\[Daily News\]\(https://h/_cards/\w+\.png\?v=\w+\)\]\(https://h/news/\)",
        line,
    )


class FakeStore:
    site_ = Site(
        "news", "Daily News", "Headlines.", "https://h/news/", ["editions"], ""
    )
    entries_ = (
        Entry("editions", "b", "Today", "2026-10-06", "https://h/news/editions/b/"),
        Entry("editions", "a", "Yesterday", "2026-10-05", "https://h/news/editions/a/"),
    )

    def sites(self):
        return [self.site_]

    def site(self, name):
        return self.site_

    def entries(self, site, section=""):
        return self.entries_

    def get(self, site, section, slug):
        [entry] = [e for e in self.entries_ if e.slug == slug]
        return entry, {"summary": f"About {entry.title}"}, "Body."


@pytest.fixture
def fake_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_store", FakeStore())
    monkeypatch.setattr(tools, "_site_dir", tmp_path)


def test_the_tools_give_cards_for_sites_and_entries(fake_tools):
    assert "Card: [![Daily News](https://h/_cards/" in tools.list_sites()
    listing = tools.list_entries("news")
    assert listing.count("Card") == 1
    assert "Card for the newest: [![Today](" in listing
    card = json.loads(tools.get_entry("news", "editions", "a"))["card"]
    assert card.startswith("[![Yesterday](https://h/_cards/")
    assert card.endswith("](https://h/news/editions/a/)")
