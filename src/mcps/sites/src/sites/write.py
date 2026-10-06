"""sites-write: save one entry and build its site, for writers outside the MCP server.

Writers in other languages can use it so the entry format and the build have one
implementation; Python writers (articles, research) call SiteStore directly.
Reads the entry as JSON on stdin:
  {"site", "section", "title", "date", "extra"?, "body"?, "slug"?}
Without a slug, one is made from the title, with -2, -3, ... when it's taken.
Prints {"url", "slug", "published": true, "error": null} as JSON once the entry is saved
and live. Exits 1, printing {"error"}, when it isn't: a bad request, or a site that
didn't build with the entry ("not saved: the site didn't build: ..."), in which case
nothing is kept, so the caller should hold on to its own copy.
"""

import json
import sys

from sites.build import Builder
from sites.store import SiteError, SiteStore


def main() -> None:
    builder = Builder.from_env()
    store = SiteStore(builder.source, builder.content, build=builder.build, agent=True)
    try:
        req = json.load(sys.stdin)
        site, section, title = req["site"], req["section"], req["title"]
        entry = store.write(
            site,
            section,
            req.get("slug"),
            title,
            req["date"],
            req.get("extra") or {},
            req.get("body") or "",
        )
    except (SiteError, KeyError, TypeError, json.JSONDecodeError) as e:
        print(
            json.dumps(
                {"error": str(e) if isinstance(e, SiteError) else f"bad request: {e!r}"}
            )
        )
        sys.exit(1)
    # published/error stay for callers that read them; a returned entry is always live.
    print(
        json.dumps(
            {"url": entry.url, "slug": entry.slug, "published": True, "error": None}
        )
    )


if __name__ == "__main__":
    main()
