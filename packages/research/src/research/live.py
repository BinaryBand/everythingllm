"""Live progress cards for deep research runs, served by research-runner on its own port.

When a run starts, the skill hands the agent a card line to paste:

    [![Deep research: <question>](https://<host>:8445/_live/research/<id>.png)](https://<host>:8445/_live/research/<id>)

`tailscale serve` maps https://<host>:8445/_live/research/ to this server (see
`make serve-setup`), as it does /news/write for sites' article writer, and strips that
prefix on the way, so paths are taken with or without it.

- `<id>.png` is the card: a chatimage.progress frame, pushed again whenever the run moves
  on (chatimage.live), at most one a second, until the run ends or MAX_STREAM passes. A
  connection watching it counts as someone following the run. A run this runner no
  longer holds (finished over an hour ago, or from before a restart) gets one frame of
  how it ended, from the run log.
- `<id>` is where the card links: the report once it's published, until then a page that
  lists what the run has done and reloads itself every few seconds (no script).

Config (environment, from host.env and the unit):
  RESEARCH_LIVE_PORT   port on 127.0.0.1 to listen on (default 8450)
  PUBLIC_HOST          the tailnet name in the card's URLs (no card without it)
"""

import asyncio
import html
import logging
import re
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from chatimage import alt, link, live, progress

from research.runlog import find

if TYPE_CHECKING:
    from research.runner import Run, Runner

log = logging.getLogger("research-runner")

PATH = "/_live/research/"
# A run's card (.png) or its link, with or without PATH: tailscale serve strips it.
ROUTE = re.compile(rf"(?:{re.escape(PATH.rstrip('/'))})?/(dr-[0-9a-f]{{8}})(\.png)?")
MAX_STREAM = 30 * 60  # seconds one connection is pushed frames; a reload asks again
GAP = 1.0  # seconds between frames at least
TICK = 30  # seconds between frames at most, so the minutes in the label move on
REFRESH_SECONDS = 4
LABEL = "Deep research"
# The state a run log line's status is drawn in.
STATES = {"ok": "done", "failed": "failed", "interrupted": "interrupted"}


def card(pages_url: str, run_id: str, question: str) -> str:
    """The Markdown line that shows a run's live card as a link; "" without a public URL."""
    if not pages_url:
        return ""
    page = f"{pages_url.rstrip('/')}{PATH}{run_id}"
    return f"[![{alt(f'{LABEL}: {question}')}]({link(page + '.png')})]({link(page)})"


class Live:
    def __init__(self, runner: "Runner"):
        self.runner = runner

    async def serve(self, port: int) -> asyncio.Server:
        return await asyncio.start_server(self.handle, "127.0.0.1", port)

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            method, path = await asyncio.wait_for(live.read_request(reader), 10)
        except (live.BadRequest, TimeoutError):
            return await live.send(writer, "400 Bad Request", b"Bad request.\n")
        if method != "GET":
            return await live.send(writer, "405 Method Not Allowed", b"GET only.\n")
        route = ROUTE.fullmatch(path)
        if not route:
            return await live.send(writer, "404 Not Found", b"No such run.\n")
        run_id, image = route[1], bool(route[2])
        run = self.runner.runs.get(run_id)
        try:
            if image and run:
                await live.push(writer, self.frames(run))
            elif image:
                await live.send(
                    writer, "200 OK", self.logged_frame(run_id), "image/png"
                )
            else:
                await self.page(writer, run_id, run)
        except Exception:  # one viewer's trouble mustn't reach the runs
            log.exception("live card for %s failed", run_id)

    async def frames(self, run: "Run") -> AsyncIterator[bytes]:
        """A frame now, then one whenever the run moves on, until it ends or MAX_STREAM."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + MAX_STREAM
        shown = None
        run.waiters += 1
        try:
            while True:
                now = (len(run.events), run.fraction, run.done, run.minutes())
                if now != shown:
                    shown = now
                    yield await asyncio.to_thread(frame, run)
                if run.done or loop.time() >= deadline:
                    return
                await asyncio.sleep(GAP)
                run.changed.clear()
                if (len(run.events), run.fraction, run.done) != now[:3]:
                    continue
                try:
                    await asyncio.wait_for(
                        run.changed.wait(), min(TICK, deadline - loop.time())
                    )
                except TimeoutError:
                    pass
        finally:
            run.waiters -= 1
            run.last_seen = time.monotonic()

    def logged_frame(self, run_id: str) -> bytes:
        """One frame of how a run this runner doesn't hold ended, from the run log."""
        record = find(self.runner.settings.runlogs, run_id)
        if record is None:
            return progress.draw(
                "This run isn't known here",
                LABEL,
                None,
                "It may be older than the run log keeps; the research site has every report.",
                "interrupted",
            )
        state = STATES.get(record.get("status"), "interrupted")
        minutes = max(1, round((record.get("seconds") or 0) / 60))
        return progress.draw(
            record.get("title") or record.get("question", ""),
            f"{LABEL} · {state} · {minutes} min",
            None if state != "done" else 1.0,
            ended_line(state, record.get("error"), bool(record.get("url"))),
            state,
        )

    async def page(
        self, writer: asyncio.StreamWriter, run_id: str, run: "Run | None"
    ) -> None:
        """The card's link: the report once it's out, else a page of how the run goes."""
        if run is not None:
            url, question, done = run.url, run.question, run.done
            events = run.events[-12:]
            status = "done" if done else "running"
        else:
            record = find(self.runner.settings.runlogs, run_id) or {}
            url, question, done = record.get("url"), record.get("question", ""), True
            events = [e[1] for e in record.get("events", [])[-12:]]
            status = STATES.get(record.get("status"), "not known here")
        if url:
            return await live.send(writer, "302 Found", headers={"Location": url})
        research = (self.runner.settings.pages_url or "/").rstrip("/") + "/research/"
        items = "".join(f"<li>{html.escape(e)}</li>" for e in events)
        refresh = (
            "" if done else f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">'
        )
        body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh}
<title>{html.escape(LABEL)} · {html.escape(status)}</title>
</head>
<body>
<h1>{html.escape(question or LABEL)}</h1>
<p>{html.escape(LABEL)}: {html.escape(status)}.{"" if done else " This page opens the report when it's published."}</p>
<ol>{items}</ol>
<p><a href="{html.escape(research)}">Every report is on the research site.</a></p>
</body>
</html>
"""
        await live.send(writer, "200 OK", body.encode(), "text/html; charset=utf-8")


def frame(run: "Run") -> bytes:
    if not run.done:
        return progress.draw(
            run.title,
            f"{LABEL} · running · {run.minutes()} min",
            run.fraction,
            run.events[-1] if run.events else "Starting.",
        )
    state = "done" if (run.result or {}).get("status") == "ok" else "failed"
    error = (run.result or {}).get("error") if state == "failed" else ""
    return progress.draw(
        run.title,
        f"{LABEL} · {state} · {run.minutes()} min",
        1.0 if state == "done" else run.fraction,
        ended_line(state, error, bool(run.url)),
        state,
    )


def ended_line(state: str, error: str | None, published: bool) -> str:
    if state == "done":
        return (
            "Published: open the report"
            if published
            else "Finished, but not published: see the chat"
        )
    if state == "failed":
        return error or "The run failed."
    return "Cut short by a restart of the research service."
