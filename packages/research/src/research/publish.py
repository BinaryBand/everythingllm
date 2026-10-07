"""Where a finished report goes: a Markdown file in the agent's filesystem folder
(anythingllm-fs), an entry on the research site, and optionally a document embedded into
the workspace that ran the research, so later chats can search it.

research-runner, which reads the web, holds no AnythingLLM login: it writes the document
(`write_document`) and asks audit-runner on the host to embed it (`ask_embed`, over a
socket that does nothing else, audit.embed), which does it with `embed_document`."""

import json
import re
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import hostrpc
import httpx
from sites.store import Entry, unique_slug


class EmbedError(RuntimeError):
    pass


# research-runner writes a report's document into AnythingLLM's documents/INCOMING/, and the
# embedder moves it to documents/FOLDER/, which no container can write, before embedding it
# there (so it can't be swapped for a symlink to another document meanwhile).
INCOMING = "deep-research-incoming"
FOLDER = "deep-research"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,150}-[0-9a-f-]{36}\.json")


def report_file(
    title: str, date: str, question: str, url: str | None, markdown: str
) -> str:
    """The report as one Markdown file: title, what was asked, where it's published, then the report."""
    where = f"Published at {url}" if url else "Not published on the research site."
    return f"# {title}\n\n_{date} · deep research on: {question}_\n\n{where}\n\n{markdown.strip()}\n"


def free_file_slug(dir: Path, title: str) -> str:
    """The title's slug (as the site makes it), with -2, -3, ... when <dir>/<slug>.md is taken."""
    return unique_slug(title, lambda slug: (dir / f"{slug}.md").exists())


def save_report_file(dir: Path, slug: str, text: str) -> Path:
    """Write the report to <dir>/<slug>.md, replacing an older copy with that slug."""
    dir.mkdir(parents=True, exist_ok=True)
    file = dir / f"{slug}.md"
    file.write_text(text, encoding="utf-8")
    return file


def save_then_publish(
    dir: Path,
    title: str,
    text: Callable[[str | None], str],
    publish: Callable[[], Entry],
) -> dict:
    """Save the report file, then publish it. The file comes first so a failed publish (the
    site doesn't keep an entry it couldn't build) doesn't lose the report; once published,
    the file is rewritten with the link. Returns {file?, file_error?, build?, publish_error?}."""
    out: dict = {}
    slug = None
    try:
        slug = free_file_slug(dir, title)
        out["file"] = str(save_report_file(dir, slug, text(None)))
    except OSError as e:
        out["file_error"] = str(e)
    try:
        out["build"] = publish()
    except Exception as e:  # noqa: BLE001 - the report is saved; any publish failure is reported
        out["publish_error"] = str(e)
        return out
    if slug is not None and "file" in out:
        try:
            save_report_file(dir, slug, text(out["build"].url))
        except OSError:
            pass  # the copy without the link stays
    return out


def published_stamp(now: datetime) -> str:
    """As AnythingLLM's own documents date themselves (toLocaleString): 10/4/2026, 5:24:00 PM."""
    return f"{now.month}/{now.day}/{now.year}, {now.hour % 12 or 12}:{now:%M:%S %p}"


# The native embedder runs in a worker that finishes after update-embeddings has answered;
# the workspace is looked at 2, 4, 8 and then every 10 s after that.
EMBED_WAIT = 120
EMBED_POLL = 2
EMBED_POLL_MAX = 10


def write_document(
    documents_dir: Path,
    folder: str,
    slug: str,
    title: str,
    url: str,
    text: str,
    now: datetime | None = None,
) -> str:
    """Store the report as an AnythingLLM document under <documents_dir>/<folder>/, as the
    UI's document picker does; its docpath, for embedding."""
    id = str(uuid.uuid4())
    docpath = f"{folder}/{slug}-{id}.json"
    doc = {
        "id": id,
        "url": url,
        "title": title,
        "docAuthor": "deep-research",
        "description": f"Deep research report: {title}",
        "docSource": "a research report written by the deep-research skill.",
        "chunkSource": f"link://{url}",
        "published": published_stamp(now or datetime.now().astimezone()),
        "wordCount": len(text.split()),
        "pageContent": text,
        "token_count_estimate": -(-len(text) // 4),
    }
    (documents_dir / folder).mkdir(parents=True, exist_ok=True)
    (documents_dir / docpath).write_text(
        json.dumps(doc, indent=4, ensure_ascii=False), encoding="utf-8"
    )
    return docpath


def ask_embed(socket: Path, workspace: str, docpath: str) -> str:
    """Have audit-runner embed the document at `docpath` (in INCOMING) into the workspace
    with that slug (audit.embed): where it is now, in FOLDER. EmbedError when it wasn't;
    the document file stays either way, so it can still be embedded from the workspace's
    settings."""
    try:
        done = hostrpc.request_sync(
            socket,
            "embed_report",
            {"workspace": workspace, "docpath": docpath},
            EMBED_WAIT + 30,
            name="the embedder",
        )
    except hostrpc.RunnerError as e:
        raise EmbedError(str(e)) from None
    return done["docpath"]


def embed_document(
    workspace: str,
    docpath: str,
    api: str,
    client: httpx.Client | None = None,
    wait: float = EMBED_WAIT,
    poll: float = EMBED_POLL,
    login: Callable[[bool], dict[str, str]] = lambda fresh: {},
) -> str:
    """Embed the document at `docpath` into the workspace with that slug, through
    AnythingLLM's API at `api`, and wait up to `wait` seconds for the workspace to list it.
    `login(fresh)` gives the headers for AnythingLLM's password (hostrpc.anythingllm_headers).
    Returns the docpath. Raises EmbedError when it wasn't embedded. On the host only
    (audit.embed): it needs AnythingLLM's password."""
    own = client is None
    client = client or httpx.Client(timeout=300)
    base = f"{api.rstrip('/')}/workspace/{quote(workspace, safe='')}"
    try:
        for fresh in (False, True):  # a login lasts 30 days: once more after a 401
            client.headers.update(login(fresh))
            resp = client.post(
                f"{base}/update-embeddings", json={"adds": [docpath], "deletes": []}
            )
            if resp.status_code != 401 or "Authorization" not in client.headers:
                break
        if resp.status_code != 200:
            raise EmbedError(
                f"AnythingLLM answered {resp.status_code} for workspace '{workspace}'"
            )
        if message := (resp.json() or {}).get("message"):
            raise EmbedError(str(message).strip())
        # The native embedder doesn't say what it embedded, and finishes later, so look.
        deadline = time.monotonic() + wait
        while True:
            listed = client.get(base)
            documents = ((listed.json() or {}).get("workspace") or {}).get(
                "documents"
            ) or []
            if docpath in {d.get("docpath") for d in documents if isinstance(d, dict)}:
                return docpath
            if time.monotonic() >= deadline:
                raise EmbedError(f"AnythingLLM didn't embed it within {wait:.0f} s")
            time.sleep(poll)
            poll = min(poll * 2, EMBED_POLL_MAX)
    except httpx.HTTPError as e:
        raise EmbedError(
            f"couldn't reach AnythingLLM: {e or type(e).__name__}"
        ) from None
    except ValueError:
        raise EmbedError("AnythingLLM's answer wasn't JSON") from None
    finally:
        if own:
            client.close()
