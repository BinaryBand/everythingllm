"""Live progress cards for a service's runs, served by the service on a port of its own.

When a run starts, the service hands its caller a card line to paste (`Live.card`):

    [![<LABEL>: <subject>](https://<host>:8445<PATH><id>.png)](https://<host>:8445<PATH><id>)

The machine routes https://<host>:8445<PATH> to the service's port (see
`uv run hostctl routes`), stripping that prefix or not, so paths are taken with or without it.

- `<id>.png` is the card: a chatimage.progress frame, pushed again whenever the run moves
  on (chatimage.live), at most one every GAP seconds, until the run ends or MAX_STREAM
  passes. A connection watching it counts as someone following the run. A run the service
  no longer holds (finished over an hour ago, or from before a restart) gets one frame of
  how it ended, from the run log. `<id>.png?theme=light` draws it in the light theme
  (chatimage.live.theme), for a client in a light theme.
- `<id>` is where the card links: `destination` (e.g. a published report) once there is
  one, until then a page of the run (`body`) that reloads itself every few seconds while
  it goes (no script). Pages are sent with a CSP that allows nothing but their own inline
  CSS, and everything a run says is escaped on them: a run's text is model output.
- `<id>.json` is the run as a client app draws it itself (`Live.status`): its subject and
  title, `state` (running, done, failed, interrupted, or unknown when neither the service
  nor its log has it), `fraction`, `minutes`, `started`, its last steps, the card's last
  line (the latest step, or how it ended and where its result is), `url` and `error`.
  Readable from any origin, as the card is: it says what the run's page says, to whoever
  has the run's id. Asking counts as following the run, as watching the card does.

A service subclasses Live and sets PATH, LABEL and its wording (`ended_line`, `body`). A run's
id is the service's ID_PREFIX and 8 hex digits (RunService.new_run).

A connection from anywhere but loopback or the server's own address is refused with a 403
(hostrpc.local_peer): in a container, that's another container on egress-net.

Config (environment):
  LIVE_HOST  the address to listen on (default 127.0.0.1). A service in a container sets
             0.0.0.0: its port is published on the host's 127.0.0.1, and what comes
             through arrives from the container's own address, not its loopback.
"""

import asyncio
import html
import json
import os
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import hostrpc
from chatimage import linked_image, live, progress

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
        # A run's card (.png), its JSON or its link, with or without PATH, as the machine's
        # route may strip it.
        self.route = re.compile(
            rf"(?:{re.escape(self.PATH.rstrip('/'))})?/({re.escape(service.ID_PREFIX)}[0-9a-f]{{8}})(\.png|\.json)?"
        )

    @classmethod
    def page_url(cls, pages_url: str, run_id: str) -> str:
        """The run's page, which its card links to; "" without a public URL."""
        return f"{pages_url.rstrip('/')}{cls.PATH}{run_id}" if pages_url else ""

    @classmethod
    def card_line(cls, pages_url: str, run_id: str, subject: str) -> str:
        """The Markdown line that shows a run's live card as a link; "" without a public URL."""
        if not pages_url:
            return ""
        page = cls.page_url(pages_url, run_id)
        return linked_image(f"{cls.LABEL}: {subject}", page + ".png", page)

    async def serve(self, port: int) -> asyncio.Server:
        host = os.environ.get("LIVE_HOST") or HOST
        return await asyncio.start_server(self.handle, host, port)

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            method, path, query = await asyncio.wait_for(live.read_request(reader), 10)
        except (live.BadRequest, TimeoutError):
            return await live.send(writer, "400 Bad Request", b"Bad request.\n")
        if not hostrpc.local_peer(
            writer.get_extra_info("peername"), writer.get_extra_info("sockname")
        ):
            return await live.send(writer, "403 Forbidden", b"Not from here.\n")
        if method != "GET":
            return await live.send(writer, "405 Method Not Allowed", b"GET only.\n")
        route = self.route.fullmatch(path)
        if not route:
            return await live.send(writer, "404 Not Found", b"No such run.\n")
        run_id, kind = route[1], route[2]
        run = self.service.runs.get(run_id)
        theme = live.theme(query)
        try:
            if kind == ".json":
                await self.send_status(writer, run_id, run)
            elif kind == ".png" and run:
                await live.push(writer, self.frames(run, theme))
            elif kind == ".png":
                frame = await asyncio.to_thread(self.logged_frame, run_id, theme)
                await live.send(writer, "200 OK", frame, "image/png")
            else:
                await self.page(writer, run_id, run)
        except Exception:  # one viewer's trouble mustn't reach the runs
            self.service.log.exception("live card for %s failed", run_id)
            if not writer.is_closing():  # nothing was sent yet; don't leave it hanging
                await live.send(
                    writer, "500 Internal Server Error", b"Something went wrong.\n"
                )

    async def frames(self, run: Run, theme: str) -> AsyncIterator[bytes]:
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
                    yield await asyncio.to_thread(self.frame, run, theme)
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

    def frame(self, run: Run, theme: str) -> bytes:
        return self.draw(self.snapshot(run.id, run, None)[0], theme)

    def logged_frame(self, run_id: str, theme: str) -> bytes:
        """One frame of how a run this service doesn't hold ended, from the run log."""
        view, _ = self.snapshot(run_id, None, find(self.runlogs, run_id))
        if view["state"] == "unknown":
            return progress.draw(
                "This run isn't known here",
                self.LABEL,
                None,
                view["line"],
                "interrupted",
                theme,
            )
        return self.draw(view, theme)

    def draw(self, view: dict[str, Any], theme: str) -> bytes:
        state = view["state"]
        return progress.draw(
            view["title"],
            f"{self.LABEL} · {state} · {view['minutes']} min",
            view["fraction"],
            view["line"],
            state,
            theme,
        )

    def snapshot(
        self, run_id: str, run: Run | None, record: dict[str, Any] | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The run as its card, page and `<id>.json` say it (the JSON's fields), and its
        result: the run held, else its run log line `record`, else unknown."""
        if run is not None:
            result = run.result or {}
            state = self.state_of(run.result) if run.done else "running"
            subject, title, started = run.subject, run.title, run.started
            minutes, fraction = run.minutes(), run.fraction
            steps = run.events[-12:]
            if run.done:
                line = self.ended_line(state, result)
            else:
                line = run.events[-1] if run.events else "Starting."
        elif record is not None:
            result = record
            state = self.STATES.get(record.get("status", ""), "interrupted")
            subject = self.subject_of(record)
            title = record.get("title") or subject
            started = record.get("started")
            minutes = max(1, round((record.get("seconds") or 0) / 60))
            fraction = None
            steps = [e[1] for e in record.get("events", [])[-12:]]
            line = self.ended_line(state, record)
        else:
            result, state, subject, title, started = {}, "unknown", "", "", None
            minutes, fraction, steps, line = None, None, [], self.unknown_line()
        ended = state not in ("running", "unknown")
        view = {
            "id": run_id,
            "kind": self.LABEL,
            "subject": subject,
            "title": title,
            "state": state,
            "fraction": 1.0 if state == "done" else fraction,
            "minutes": minutes,
            "started": started,
            "steps": steps,
            "line": line,
            "url": (self.destination(result) if ended else None) or None,
            "error": (result.get("error") if state == "failed" else None) or None,
        }
        return view, result

    async def logged(self, run_id: str, run: Run | None) -> dict[str, Any] | None:
        """The run's log line when this service doesn't hold it, read in a thread."""
        return None if run else await asyncio.to_thread(find, self.runlogs, run_id)

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

    async def send_status(
        self, writer: asyncio.StreamWriter, run_id: str, run: Run | None
    ) -> None:
        if run:
            run.last_seen = time.monotonic()  # a client polling it is following it
        view, _ = self.snapshot(run_id, run, await self.logged(run_id, run))
        await live.send(
            writer,
            "200 OK",
            json.dumps(view, ensure_ascii=False).encode(),
            "application/json",
            headers=live.CORS,
        )

    async def page(
        self, writer: asyncio.StreamWriter, run_id: str, run: Run | None
    ) -> None:
        """The card's link: its destination once there is one, else a page of the run."""
        view, result = self.snapshot(run_id, run, await self.logged(run_id, run))
        if url := view["url"]:
            return await live.send(writer, "302 Found", headers={"Location": url})
        subject, events = view["subject"], view["steps"]
        done = view["state"] != "running"
        status = "not known here" if view["state"] == "unknown" else view["state"]
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
