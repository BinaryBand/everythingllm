"""Live progress cards for deep research runs (runs.live), served by research-runner on its
own port, at https://<host>:8445/_live/research/<id>.png and its link. The link opens the
report once it's published, and until then a page of the run's progress.

Config (environment, from host.env and the unit):
  RESEARCH_LIVE_PORT   port on 127.0.0.1 to listen on (default 8450)
  PUBLIC_HOST          the tailnet name in the card's URLs (no card without it)
"""

import html
from typing import Any

from runs import live


class Live(live.Live):
    PATH = "/_live/research/"
    LABEL = "Deep research"

    def subject_of(self, record: dict[str, Any]) -> str:
        return str(record.get("question") or record.get("subject") or "")

    def ended_line(self, state: str, result: dict[str, Any]) -> str:
        if state == "done":
            return (
                "Published: open the report"
                if result.get("url")
                else "Finished, but not published: see the chat"
            )
        if state == "failed":
            return result.get("error") or "The run failed."
        return "Cut short by a restart of the research service."

    def unknown_line(self) -> str:
        return "It may be older than the run log keeps; the research site has every report."

    def body(
        self,
        subject: str,
        status: str,
        done: bool,
        events: list[str],
        result: dict[str, Any],
    ) -> str:
        research = (self.pages_url or "/").rstrip("/") + "/research/"
        items = "".join(f"<li>{html.escape(e)}</li>" for e in events)
        opens = "" if done else " This page opens the report when it's published."
        return (
            f"<h1>{html.escape(subject or self.LABEL)}</h1>\n"
            f"<p>{html.escape(self.LABEL)}: {html.escape(status)}.{opens}</p>\n"
            f"<ol>{items}</ol>\n"
            f'<p><a href="{html.escape(research)}">Every report is on the research site.</a></p>'
        )
