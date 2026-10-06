"""Link cards for the sites (see linkcard): the Markdown line the agent pastes to show a
site's home page or an entry in the chat as a big link. Host-only, like linkcard: the
sites tools in sites-runner and deep research use it, the MCP server in the container
doesn't.
"""

import re
from pathlib import Path

import linkcard

from sites.store import Entry, Site, SiteStore

# Front-matter fields that say in a line what an entry is, in the order they're tried.
DESCRIBED_BY = ("summary", "description", "question")


def entry_card(
    site_dir: Path, store: SiteStore, site: str, entry: Entry, extra: dict, body: str
) -> str:
    """The card line for `entry`, just written to `site`; "" when it couldn't be made,
    since the entry is published either way."""
    try:
        title = store.site(site).title
    except Exception:  # noqa: BLE001 - the site's own name will do on the card
        title = site
    return linkcard.make(
        site_dir,
        entry.url,
        entry.title,
        f"{title} · {entry.date}",
        describe(extra, body),
    )


def site_card(site_dir: Path, site: Site) -> str:
    """The card line for a site's home page; "" when it couldn't be made."""
    return linkcard.make(
        site_dir, site.url, site.title, f"{site.title} · home", site.description
    )


def describe(extra: dict, body: str) -> str:
    """A line about the entry: a summary field if it has one, else the body's first
    paragraph of prose, as plain text."""
    for key in DESCRIBED_BY:
        if isinstance(value := extra.get(key), str) and value.strip():
            return value
    for block in re.split(r"\n\s*\n", body):
        text = block.strip()
        if not text or re.match(r"(#|```|~~~|\||[-*+] |\d+\. |<|!\[)", text):
            continue
        text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links and images
        text = re.sub(r"\[\^?[^\]]*\]", "", text)  # footnote and citation marks
        return re.sub(r"[*_`>]+", "", text)
    return ""
