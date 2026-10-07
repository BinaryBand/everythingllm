"""Embedding a deep-research report into its workspace, for research-runner, which reads the
web in its service container and so holds no AnythingLLM login. It writes the report's
document into storage's documents/deep-research-incoming/ and asks here, on a socket of its
own that serves this op and nothing else (AUDIT_EMBED_SOCKET; audit-runner serves it beside
its own). This moves the document to documents/deep-research/, which no container can
write, reading it without following a symlink, so what AnythingLLM embeds is what was
checked; then makes the call with AnythingLLM's password (research.publish.embed_document).

So a research-runner that a page took over can at most have a report of its own embedded
into a workspace, not log in to AnythingLLM.

Config (environment, as audit-runner's):
  AUDIT_EMBED_SOCKET   the socket (default <storage>/everythingllm/research-embed/runner.sock),
                       which research-runner's container mounts as RESEARCH_EMBED_SOCKET
  ANYTHINGLLM_API, ANYTHINGLLM_ENV  as audit.tools
"""

import logging
import os
import re
from pathlib import Path
from typing import Any

import hostrpc
from hostrpc import RunnerError, safefs
from research.publish import FOLDER, INCOMING, NAME_RE, EmbedError, embed_document

log = logging.getLogger("audit-embed")

# AnythingLLM's workspace slugs: lower case, digits and hyphens.
WORKSPACE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}")
DOCUMENT_BYTES = 4 << 20  # a report's document, at most


class Embedder(hostrpc.Service):
    """The embed socket's one op."""

    log = log

    def __init__(self, storage: Path, api: str, env_file: Path):
        super().__init__()
        self.storage, self.api, self.env_file = storage, api, env_file

    @classmethod
    def from_env(cls) -> "Embedder":
        storage = hostrpc.storage()
        get = os.environ.get
        return cls(
            storage,
            get("ANYTHINGLLM_API", "http://127.0.0.1:3001/api").rstrip("/"),
            Path(get("ANYTHINGLLM_ENV", storage / ".env")),
        )

    def op_embed_report(self, workspace: str, docpath: str) -> dict[str, Any]:
        """Embed the deep-research document research-runner left at `docpath`
        (deep-research-incoming/<name>, in storage's documents/) into the workspace with
        that slug: {docpath}, where it is now."""
        workspace, docpath = str(workspace or ""), str(docpath or "")
        if not WORKSPACE_RE.fullmatch(workspace):
            raise RunnerError(f"bad workspace '{workspace[:100]}'")
        folder, _, name = docpath.partition("/")
        if folder != INCOMING or not NAME_RE.fullmatch(name):
            raise RunnerError(
                f"only a deep-research report's document is embedded here, {INCOMING}/<slug>-<id>.json"
            )
        moved = f"{FOLDER}/{name}"
        self.move(name)
        try:
            embed_document(
                workspace,
                moved,
                self.api,
                login=lambda fresh: hostrpc.anythingllm_headers(
                    self.api, self.env_file, fresh=fresh
                ),
            )
        except EmbedError as e:
            raise RunnerError(str(e)) from None
        log.info("embedded %s into %s", moved, workspace)
        return {"docpath": moved}

    def move(self, name: str) -> None:
        """documents/INCOMING/<name> to documents/FOLDER/<name>: a plain file only, read
        without following a symlink and written new."""
        documents = self.storage / "documents"
        try:
            with safefs.folder(documents, (INCOMING,)) as incoming:
                try:
                    fd = safefs.open_regular(incoming, name)
                except OSError:
                    raise RunnerError(
                        f"there's no document {INCOMING}/{name}"
                    ) from None
                with os.fdopen(fd, "rb") as f:
                    data = f.read(DOCUMENT_BYTES + 1)
                if len(data) > DOCUMENT_BYTES:
                    raise RunnerError(f"{name} is over {DOCUMENT_BYTES >> 20} MB")
                with safefs.folder(documents, (FOLDER,), make=True) as dest:
                    out = safefs.create(dest, name)
                    with os.fdopen(out, "wb") as f:
                        f.write(data)
                os.unlink(name, dir_fd=incoming)
        except FileExistsError:
            raise RunnerError(f"{FOLDER}/{name} is there already") from None
        except OSError as e:
            raise RunnerError(f"couldn't move {name} for embedding: {e}") from None


def socket() -> Path:
    return hostrpc.socket_path("research-embed", "AUDIT_EMBED_SOCKET")
