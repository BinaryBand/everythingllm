"""Live progress cards for a service's runs, served by the service on a port of its own.

When a run starts, the service hands its caller a card line to paste (`Live.card`):

    [![<LABEL>: <subject>](https://<host>:8445<PATH><id>.png)](https://<host>:8445<PATH><id>)

`tailscale serve` maps https://<host>:8445<PATH> to the service's port (see
`uv run hostctl serve-setup`) and strips that prefix on the way, so paths are taken with or without it.

- `<id>.png` is the card: a chatimage.progress frame, pushed again whenever the run moves
  on (chatimage.live), at most one every GAP seconds, until the run ends or MAX_STREAM
  passes. A connection watching it counts as someone following the run. A run the service
  no longer holds (finished over an hour ago, or from before a restart) gets one frame of
  how it ended, from the run log.
- `<id>` is where the card links: `destination` (e.g. a published report) once there is
  one, until then a page of the run (`body`) that reloads itself every few seconds while
  it goes (no script). Pages are sent with a CSP that allows nothing but their own inline
  CSS, and everything a run says is escaped on them: a run's text is model output.

A service subclasses Live and sets PATH, LABEL and its wording (`ended_line`, `body`). A run's
id is the service's ID_PREFIX and 8 hex digits (RunService.new_run).

Config (environment):
  LIVE_HOST  the address to listen on (default 127.0.0.1). A service in a container sets
             0.0.0.0: its port is published on the host's 127.0.0.1, and what comes
             through arrives from the container's own address, not its loopback.
"""

import asyncio
import html
import os
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

from chatimage import alt, link, live, progress

from runs.runlog import find
from runs.service import Run, RunService

HOST = "127.0.0.1"  # where the cards listen unless LIVE_HOST says otherwise
CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


class Live:
    PATH = "/_live/run/"
    LABEL = "Run"
    MAX_STREAM = 30 * 60  # seconds one connection is pushed frames; a reload asks again
    GAP = 1.0  # seconds between frames at least
    TICK = 30  # seconds between frames at most, so the minutes in the label move on
    REFRESH_SECONDS = 4
    # The state a result's or a run log line's status is drawn in.
    STATES: ClassVar[dict[str, str]] = {
        "ok": "done",
        "failed": "failed",
        "interrupted": "interrupted",
    }

    def __init__(self, service: RunService, runlogs: Path, pages_url: str):
        self.service = service
        self.runlogs = runlogs
        self.pages_url = pages_url
        # A run's card (.png) or its link, with or without PATH: tailscale serve strips it.
        self.route = re.compile(
            rf"(?:{re.escape(self.PATH.rstrip('/'))})?/({re.escape(service.ID_PREFIX)}[0-9a-f]{{8}})(\.png)?"
        )

    @classmethod
    def card_line(cls, pages_url: str, run_id: str, subject: str) -> str:
        """The Markdown line that shows a run's live card as a link; "" without a public URL."""
        if not pages_url:
            return ""
        page = f"{pages_url.rstrip('/')}{cls.PATH}{run_id}"
        return f"[![{alt(f'{cls.LABEL}: {subject}')}]({link(page + '.png')})]({link(page)})"

    async def serve(self, port: int) -> asyncio.Server:
        host = os.environ.get("LIVE_HOST") or HOST
        return await asyncio.start_server(self.handle, host, port)

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            method, path = await asyncio.wait_for(live.read_request(reader), 10)
        except (live.BadRequest, TimeoutError):
            return await live.send(writer, "400 Bad Request", b"Bad request.\n")
        if method != "GET":
            return await live.send(writer, "405 Method Not Allowed", b"GET only.\n")
        route = self.route.fullmatch(path)
        if not route:
            return await live.send(writer, "404 Not Found", b"No such run.\n")
        run_id, image = route[1], bool(route[2])
        run = self.service.runs.get(run_id)
        try:
            if image and run:
                await live.push(writer, self.frames(run))
            elif image:
                frame = await asyncio.to_thread(self.logged_frame, run_id)
                await live.send(writer, "200 OK", frame, "image/png")
            else:
                await self.page(writer, run_id, run)
        except Exception:  # one viewer's trouble mustn't reach the runs
            self.service.log.exception("live card for %s failed", run_id)
            if not writer.is_closing():  # nothing was sent yet; don't leave it hanging
                await live.send(
                    writer, "500 Internal Server Error", b"Something went wrong.\n"
                )

    async def frames(self, run: Run) -> AsyncIterator[bytes]:
        """A frame now, then one whenever the run moves on, until it ends or MAX_STREAM."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.MAX_STREAM
        shown = None
        run.waiters += 1
        try:
            while True:
                now = (len(run.events), run.fraction, run.done, run.minutes())
                if now != shown:
                    shown = now
                    yield await asyncio.to_thread(self.frame, run)
                if run.done or loop.time() >= deadline:
                    return
                await asyncio.sleep(self.GAP)
                run.changed.clear()
                if (len(run.events), run.fraction, run.done) != now[:3]:
                    continue
                try:
                    await asyncio.wait_for(
                        run.changed.wait(), min(self.TICK, deadline - loop.time())
                    )
                except TimeoutError:
                    pass
        finally:
            run.waiters -= 1
            run.last_seen = time.monotonic()

    def state_of(self, result: dict[str, Any] | None) -> str:
        return self.STATES.get((result or {}).get("status", ""), "failed")

    def frame(self, run: Run) -> bytes:
        if not run.done:
            return progress.draw(
                run.title,
                f"{self.LABEL} · running · {run.minutes()} min",
                run.fraction,
                run.events[-1] if run.events else "Starting.",
            )
        state = self.state_of(run.result)
        return progress.draw(
            run.title,
            f"{self.LABEL} · {state} · {run.minutes()} min",
            1.0 if state == "done" else run.fraction,
            self.ended_line(state, run.result or {}),
            state,
        )

    def logged_frame(self, run_id: str) -> bytes:
        """One frame of how a run this service doesn't hold ended, from the run log."""
        record = find(self.runlogs, run_id)
        if record is None:
            return progress.draw(
                "This run isn't known here",
                self.LABEL,
                None,
                self.unknown_line(),
                "interrupted",
            )
        state = self.STATES.get(record.get("status", ""), "interrupted")
        minutes = max(1, round((record.get("seconds") or 0) / 60))
        return progress.draw(
            record.get("title") or self.subject_of(record),
            f"{self.LABEL} · {state} · {minutes} min",
            None if state != "done" else 1.0,
            self.ended_line(state, record),
            state,
        )

    # --- what a service says ---

    def subject_of(self, record: dict[str, Any]) -> str:
        return str(record.get("subject") or "")

    def ended_line(self, state: str, result: dict[str, Any]) -> str:
        """The card's last line once a run has ended; `result` is its result or log line."""
        if state == "done":
            return "Finished"
        if state == "failed":
            return result.get("error") or "The run failed."
        return "Cut short by a restart of the service."

    def unknown_line(self) -> str:
        return "It may be older than the run log keeps."

    def destination(self, result: dict[str, Any]) -> str | None:
        """Where the card's link goes once the run has ended, if anywhere but its page."""
        return result.get("url")

    def body(
        self,
        subject: str,
        status: str,
        done: bool,
        events: list[str],
        result: dict[str, Any],
    ) -> str:
        """The page's body (HTML; escape whatever a run said)."""
        items = "".join(f"<li>{html.escape(e)}</li>" for e in events)
        return (
            f"<h1>{html.escape(subject or self.LABEL)}</h1>\n"
            f"<p>{html.escape(self.LABEL)}: {html.escape(status)}.</p>\n<ol>{items}</ol>"
        )

    async def page(
        self, writer: asyncio.StreamWriter, run_id: str, run: Run | None
    ) -> None:
        """The card's link: its destination once there is one, else a page of the run."""
        if run is not None:
            subject, done, result = run.subject, run.done, run.result or {}
            events = run.events[-12:]
            status = self.state_of(run.result) if done else "running"
        else:
            result = await asyncio.to_thread(find, self.runlogs, run_id) or {}
            subject, done = self.subject_of(result), True
            events = [e[1] for e in result.get("events", [])[-12:]]
            status = self.STATES.get(result.get("status", ""), "not known here")
        if done and (url := self.destination(result)):
            return await live.send(writer, "302 Found", headers={"Location": url})
        refresh = (
            ""
            if done
            else f'<meta http-equiv="refresh" content="{self.REFRESH_SECONDS}">'
        )
        page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh}
<title>{html.escape(self.LABEL)} · {html.escape(status)}</title>
</head>
<body>
{self.body(subject, status, done, events, result)}
</body>
</html>
"""
        await live.send(
            writer,
            "200 OK",
            page.encode(),
            "text/html; charset=utf-8",
            headers={"Content-Security-Policy": CSP},
        )
