"""sites-runner: what the sites tools do, on the host, in a service container of its own
(host/quadlet/sites-runner.container.in; README, "Service containers"). The MCP server in
AnythingLLM's container (server.py) forwards each tool call here over hostrpc and shows the
agent the text returned. Each function in OPS is the tool of the same name; server.py describes them. A
SiteError's text is the tool's error. The entries and the zola builds happen here (the
builds through the sandbox runner), like every other site writer's (research-runner,
`uv run hostctl sites-build`); the build lock keeps them from overlapping.

Config (environment, from host.env and the unit):
  SITES_SOCKET   socket to listen on (default <storage>/everythingllm/sites/runner.sock)
  SITES_SOURCE   repo directory holding one Zola site per subdirectory
  ANYTHINGLLM_STORAGE, SITES_CONTENT, SITES_OUTPUT, SANDBOX_BUILD_SOCKET
                 where entries and built sites are, and the sandbox runner's build
                 socket; see sites.build for the defaults
"""

import json
import logging
from pathlib import Path
from typing import Any

import chatimage.card
import hostrpc

from sites import cards
from sites.build import Builder
from sites.store import SiteError, SiteStore, today

_store: SiteStore | None = None
_site_dir: Path | None = (
    None  # the pages site's root, where the built sites and cards go
)


def store() -> SiteStore:
    global _store, _site_dir
    if _store is None:
        builder = Builder.from_env()
        _site_dir = builder.output
        _store = SiteStore(
            builder.source, builder.content, build=builder.build, agent=True
        )
    return _store


def site_dir() -> Path:
    store()
    assert _site_dir is not None
    return _site_dir


def list_sites() -> str:
    sites = store().sites()
    if not sites:
        return "No sites are set up."
    return "\n\n".join(
        f"## {s.name}: {s.title} ({s.url})\n{s.description}\n"
        f"Sections: {', '.join(n + (' (read-only)' if n in s.readonly else '') for n in s.sections) or 'none'}\n"
        + (f"Card: {card}\n" if (card := cards.site_card(site_dir(), s)) else "")
        + f"{s.help}".strip()
        for s in sites
    )


def list_entries(site: str, section: str = "", limit: int = 20) -> str:
    entries = store().entries(site, section)
    head = f"Today is {today()} in the user's time zone."
    if not entries:
        return f"{head}\nNo entries yet."
    shown = entries[:limit] if limit > 0 else entries
    lines = [
        head,
        *(f"- {e.section}/{e.slug} ({e.date}): {e.title} {e.url}" for e in shown),
    ]
    if len(shown) < len(entries):
        lines.append(
            f"({len(entries) - len(shown)} older not shown; raise limit to see them.)"
        )
    # The newest only: a card per row would crowd the listing. get_entry has the others'.
    newest = shown[0]
    try:
        _, extra, body = store().get(site, newest.section, newest.slug)
    except SiteError:
        extra, body = {}, ""
    if card := cards.entry_card(site_dir(), store(), site, newest, extra, body):
        lines.append(f"Card for the newest: {card}")
    return "\n".join(lines)


def get_entry(site: str, section: str, slug: str) -> str:
    entry, extra, body = store().get(site, section, slug)
    return json.dumps(
        {
            "title": entry.title,
            "date": entry.date,
            "url": entry.url,
            "card": cards.entry_card(site_dir(), store(), site, entry, extra, body),
            "extra": extra,
            "body": body,
        },
        ensure_ascii=False,
        indent=1,
    )


def write_entry(
    site: str,
    section: str,
    slug: str,
    title: str,
    date: str,
    extra: dict[str, Any] | None = None,
    body: str = "",
    overwrite: bool = False,
) -> str:
    entry = store().write(
        site, section, slug, title, date, extra or {}, body, overwrite
    )
    card = cards.entry_card(site_dir(), store(), site, entry, extra or {}, body)
    return f"Saved '{entry.title}'; it's live at {entry.url}" + (
        f"\nCard: {card}" if card else ""
    )


def delete_entry(site: str, section: str, slug: str) -> str:
    entry, _, _ = store().get(site, section, slug)
    store().delete(site, section, slug)
    chatimage.card.remove(site_dir(), entry.url)
    return f"Deleted {site}/{section}/{slug}."


# The tools block (files are written, zola runs), so hostrpc runs each in a thread.
OPS = (list_sites, list_entries, get_entry, write_entry, delete_entry)
# The ops that write (write_entry, delete_entry) are agent skills, not tools of the MCP
# front: an MCP call doesn't say which workspace made it, and a skill refuses a delegated
# task. sites.server declares them in its `skills`.
log = logging.getLogger("sites-runner")
# Bad arguments, missing entries, a site that didn't build.
runner = hostrpc.Service(OPS, errors=(SiteError,), log=log)


def main() -> None:
    store()  # a bad config shows in the journal now, not at the first tool call
    hostrpc.run(runner, "sites", "SITES_SOCKET")
