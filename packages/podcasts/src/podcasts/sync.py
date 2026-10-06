"""`podcasts-sync [slug]`: download new episodes for every feed (or one), cut their ads, and prune old ones.

Runs as podcasts-sync@<slug>.service, `_all` for every feed (the timer's); podcasts-runner
starts either for chat. It takes sync.lock, and exits at once if another sync is running.
A crash is recorded in last_sync.json, so list_podcasts can tell.
"""

import sys
import traceback

from podcasts.library import ALL_FEEDS, Library, make_client


def main() -> None:
    args = sys.argv[1:]
    only = args[0] if args and args[0] != ALL_FEEDS else ""
    lib = Library.from_env()
    try:
        with make_client() as client:
            ran = lib.sync(client, only)
    except Exception:
        lib.sync_crashed(traceback.format_exc())
        raise
    if not ran:
        print("another sync is running", file=sys.stderr)


if __name__ == "__main__":
    main()
