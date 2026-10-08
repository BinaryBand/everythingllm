"""Live progress cards for deep research runs (runs.live), served by research-runner on its
own port, at https://<host>:8445/_live/research/<id>.png and its link, a page of the run's
progress. The report itself goes to the chat and its workspace's documents, not a page.

Config (environment, from host.env and the unit):
  RESEARCH_LIVE_PORT   port to listen on (default 8450), on LIVE_HOST (runs.live)
  PUBLIC_HOST          the machine's HTTPS name in the card's URLs (no card without it)
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
            return "Done: the report is in the chat's workspace documents"
        if state == "failed":
            return result.get("error") or "The run failed."
        return "Cut short by a restart of the research service."

    def unknown_line(self) -> str:
        return "It may be older than the run log keeps; its report is in the agent's files."

    def body(
        self,
        subject: str,
        status: str,
        done: bool,
        events: list[str],
        result: dict[str, Any],
    ) -> str:
        items = "".join(f"<li>{html.escape(e)}</li>" for e in events)
        where = (
            " The report is in the chat that asked for it, and in its workspace's "
            "documents."
            if done
            else ""
        )
        return (
            f"<h1>{html.escape(subject or self.LABEL)}</h1>\n"
            f"<p>{html.escape(self.LABEL)}: {html.escape(status)}.{where}</p>\n"
            f"<ol>{items}</ol>"
        )
