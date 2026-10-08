"""The sandbox runner: a host daemon that runs the agent's code in throwaway podman containers
and builds the sites it makes.

Each run gets a fresh container from the sandbox image on egress-net, at one of the
egress profile `sandbox`'s addresses (packages/egress/egress.toml), so its only way out
is the egress proxy, which lets it reach PyPI and nothing else; a read-only root,
CPU/memory/process limits and a time limit. Each address is a slot: a run or a build holds
one while its container exists, so no more containers run at once than the profile has
addresses, and a stopped run's container never keeps an address another is given.

A workspace the user gave access (op_access, the sandbox-access skill, approved in
AnythingLLM's own prompt; kept in SANDBOX_ACCESS) gets more: with web access, its runs take
an address of the `sandbox-web` profile instead and go out through the proxy's public port,
public hosts only, without other workspaces' /shared folders; with model access, each run
asks a model through a socket of its own (sandbox.models), within a daily token budget,
the key staying here.

What a run can see depends on where the call came from, which the skills in AnythingLLM
pass as a scope of {workspace, thread} (never chosen by the model):

  /work            the thread's scratch folder, deleted a week after the thread last used it
  /project         the workspace's folder, shared by its threads and kept (pip installs go here)
  /shared/<ws>     the workspace's shared folder: it writes it, every other workspace reads it
  /shared/<other>  each other workspace's shared folder, read-only
  /public          the workspace's pages on the web, served as they are at
                   https://<host>:8447/<workspace>/ the moment they're written
  /system/themes   the repo's Zola themes, read-only

The browser (packages/browser) saves downloads in /project/downloads.

The shared and system folders are mounted noexec and never on PATH: they're data, and code
in another workspace's folder isn't to be run. A workspace's folders together (its shared
folder too) are held to WORKSPACE_MAX_BYTES: no run starts past it, and one that takes the
workspace past it and RUN_SLACK, or past MAX_FILES files and folders, is killed (`watch`, every
WATCH_SECONDS); no file a run writes can be over FILE_MAX_BYTES. The script itself is mounted read-only from a
host-only folder at /sandbox.

Nothing is written by more than one workspace, so the workspace's lock is all the runner
needs: a run, a write and a publish in one workspace take turns, runs in different
workspaces overlap, and the runner's file operations only ever take paths in the caller's
own folders, which no other workspace can change under them.

Each workspace's /public lives apart from its other folders, in a tree that holds nothing
but /public folders (SANDBOX_PUBLIC, `<public>/<workspace>/`). The workspace pages site
(static_agent, host/caddy/pages.Caddyfile, :8447) serves that tree read-only as it is, each
workspace under its own prefix: there's no copy, no page to claim, and a half-written page
is the workspace's own business. Nothing private is in the tree, so a symlink in it can't
reach another workspace's /project or /work. The site's CSP is one rule for every
workspace: pages may run inline and same-site scripts, but in a CSP sandbox, where each
page has an opaque origin (no storage, no reading the site's files, no forms, popups or
alerts), and nothing loads from another host. A run, write or build that changed /public
says which pages changed and where they are, what in them the CSP blocks, and its notices:
that a page has scripts (so the agent asks the user before publishing it) and which of the
sandbox's limits it runs into. op_publish gives the same for a page, with its address and
link card, and can copy a file or folder into /public first.

op_show_image puts an image from the caller's folders in the chat: a PNG, JPEG, GIF or
WebP file, read without following a symlink and named by its format in its header (never
by its name, and never an SVG, which opened by itself is a page), is copied to the pages
site's `_images/<workspace>/`, named by a hash of its bytes, so its address can't be
guessed and shows the same image for good. The reply's `image` is a Markdown image in a
link to it, which the agent pastes as it does a link card. They count toward
IMAGES_MAX_BYTES a workspace, not its size limit: they're outside its folders.

op_app keeps the workspace's apps (sandbox.apps): an app is a template from the repo plus
the workspace's data for it in /project/apps/<name>/data.json; each change goes through the
template's ops, re-renders its page in /public/apps/<name>/ with the data embedded and a
new write-back token (kept host-only in APP_STATE's folder), and moves its live card on.

A site build (op_build_site) runs the repo's sitebuild.py in a container with no network
and every folder read-only but an empty /out: it copies a Zola site from the workspace's
own folders, puts the theme it names in place (the repo's from /system/themes, or a
workspace's from /shared/<it>/themes), and builds it. The runner copies the output into
/public/<slug> (plain files only), so a site goes live like any page.

The runner's parts are modules of their own: sandbox.workspace (a call's scope, its
folders and their limits), sandbox.access, sandbox.containers (podman, and each
container's arguments), sandbox.pages (/public, and show_image's images) and
sandbox.attachments. Its config, and the environment it's read from, is
sandbox.workspace's Config.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import stat
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import chatimage
import chatimage.card
import hostrpc
from hostrpc import safefs

from sandbox import apps as app_templates
from sandbox import appsweb, pages, workspace
from sandbox import models as model_access
from sandbox.access import (
    MAX_DAILY_TOKENS,
    NO_ACCESS,
    Access,
    read_access,
    valid_budget,
    write_access,
)
from sandbox.apps.tokens import Tokens
from sandbox.attachments import sync_attachments
from sandbox.containers import (
    IMAGE,
    LABEL,
    PROXY_CONTAINER,
    Podman,
    model_dir,
    podman,
    prepare,
    prepare_build,
)
from sandbox.errors import Busy, NoSuchApp, SandboxError
from sandbox.names import SLUG
from sandbox.pages import LIST_MAX, Pages
from sandbox.workspace import (
    PROFILE,
    SLUG_RE,
    WEB_PROFILE,
    Config,
    Scope,
    existing_scope,
    gc_threads,
    make_scope,
    over_quota,
    public_changes,
    remove_path,
    resolve,
    snapshot,
    split,
    write_regular,
)

log = logging.getLogger("sandbox-runner")
# Big enough for a run's output, which the runner caps well below this.
LIMIT = 8 * 1024 * 1024

LANGUAGES = {"python": ("main.py", "python"), "bash": ("main.sh", "bash")}
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 300
BUILD_TIMEOUT = 60  # a site build, assembling included
WATCH_SECONDS = 3.0  # how often a run's workspace is looked at (watch)
APP_DATA_BYTES = 1 << 20  # an app's data.json, at most
APP_ACTIONS = ("create", "do", "show", "list", "delete")
APP_DATA_RE = re.compile(rf"/project/apps/({SLUG})/data\.json")
# A request answers within WAIT; a run that's still going carries on, and the skill waits
# on it again with op_wait.
WAIT = 45
RESULT_KEEP = 3600  # how long a finished run's result can still be fetched


@dataclass
class Job:
    """A run, kept for RESULT_KEEP after it finishes so op_wait can still fetch its result."""

    workspace: str
    task: asyncio.Task[dict[str, Any]]
    started: float
    finished: float | None = None


@dataclass
class Runner(hostrpc.Service):
    log = log

    config: Config
    podman: Podman = podman
    now: Callable[[], float] = time.time
    _free: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    _free_web: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    # Asks a model for a run with model access; by default the providers' (packages/llm).
    ask_model: model_access.Ask | None = None
    # Notified whenever an app changes, for its live card (sandbox.appsweb).
    apps_changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    _access_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _jobs: dict[str, Job] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for ip in self.config.ips:
            self._free.put_nowait(ip)
        for ip in self.config.web_ips:
            self._free_web.put_nowait(ip)
        if self.ask_model is None:
            self.ask_model = model_access.provider_ask(self.config.model_env)
        self.app_tokens = Tokens(self.config.app_state)
        self.pages = Pages(self.config)

    @contextlib.asynccontextmanager
    async def slot(self, web: bool = False) -> AsyncIterator[str]:
        """One of the sandbox profile's addresses, held until the block ends: a run's
        container takes it on egress-net, a build's (no network) only its turn. With
        `web`, one of the sandbox-web profile's, which the egress proxy lets reach public
        hosts."""
        ips, free = (
            (self.config.web_ips, self._free_web)
            if web
            else (self.config.ips, self._free)
        )
        if not ips:
            raise SandboxError(
                f"the sandbox has no addresses on egress-net (egress.toml's "
                f"{WEB_PROFILE if web else PROFILE} profile)"
            )
        ip = await free.get()
        try:
            yield ip
        finally:
            free.put_nowait(ip)

    # --- access (sandbox.access) ---

    def access(self, scope: Scope) -> Access:
        """The workspace's access, from access_file; none for a gateway client's, and none
        when the file is missing or can't be read."""
        if scope.gateway:
            return NO_ACCESS
        return read_access(self.config.access_file, scope.workspace)

    async def op_access(
        self,
        scope: dict[str, Any],
        web: Any = None,
        models: Any = None,
        daily_tokens: Any = None,
        apply: bool = False,
        approved: bool = False,
    ) -> dict[str, Any]:
        """The workspace's access, and with `web` or `models` (true or false) or
        `daily_tokens` what it would be. With `apply` it's changed: turning anything on,
        or raising the budget, also needs `approved`, the user's own approval in the chat,
        which the sandbox-access skill asks AnythingLLM for; turning it off needs nothing
        more."""
        s = self.scope(scope)
        if s.gateway:
            raise SandboxError(
                "a gateway client's sandbox reaches only PyPI; its access can't change"
            )
        for name, value in (("web", web), ("models", models)):
            if value is not None and not isinstance(value, bool):
                raise SandboxError(f"{name} must be true, false or left out")
        if daily_tokens is not None and not valid_budget(daily_tokens):
            raise SandboxError(f"daily_tokens must be 1 to {MAX_DAILY_TOKENS}")
        current = self.access(s)
        changes = {
            k: v
            for k, v in (
                ("web", web),
                ("models", models),
                ("daily_tokens", daily_tokens),
            )
            if v is not None
        }
        wanted = replace(current, **changes)
        on = (
            (wanted.web and not current.web)
            or (wanted.models and not current.models)
            or wanted.daily_tokens > current.daily_tokens
        )
        out = {"workspace": s.workspace, **asdict(current)}
        if wanted == current:
            return {**out, "changed": False}
        if apply is not True:
            return {**out, "would": asdict(wanted), "needs_approval": on}
        if on and approved is not True:
            raise SandboxError(
                "turning access on needs the user's approval in the chat, which the "
                "sandbox-access skill asks for"
            )
        async with self._access_lock:
            await asyncio.to_thread(
                write_access, self.config.access_file, s.workspace, wanted
            )
        log.info("access for %s: %s", s.workspace, asdict(wanted))
        return {**out, **asdict(wanted), "changed": True}

    # --- scopes (sandbox.workspace) ---

    def scope(self, scope: dict[str, Any]) -> Scope:
        """The caller's folders, made on first use; using the thread's /work keeps it from gc."""
        return make_scope(self.config, scope)

    def app_scope(self, workspace: str) -> Scope | None:
        """A workspace's scope for its apps, as an address names it (sandbox.appsweb): None
        if it has no sandbox or is a gateway client's. Nothing is made."""
        return existing_scope(self.config, workspace)

    def lock(self, workspace: str) -> asyncio.Lock:
        return self._locks.setdefault(workspace, asyncio.Lock())

    def idle(self, workspace: str) -> None:
        """Fail fast while a run holds the workspace (another chat's, or one whose chat
        closed), rather than queueing for its lock past the caller's patience."""
        for job in self._jobs.values():
            if job.workspace == workspace and not job.task.done():
                left = max(0, MAX_TIMEOUT - (self.now() - job.started))
                raise Busy(
                    f"code is still running in this workspace (at most {left:.0f} s more); try again after"
                )

    @contextlib.asynccontextmanager
    async def exclusive(self, workspace: str) -> AsyncIterator[None]:
        """The workspace's lock for a file operation, which fails at once (Busy) while a
        run holds it rather than queueing behind the run."""
        self.idle(workspace)
        async with self.lock(workspace):
            yield

    def gc(self) -> list[str]:
        """Delete threads' /work folders untouched for a week. /project stays."""
        return gc_threads(self.config.root, self.now())

    # --- running code ---

    async def op_ping(self) -> dict[str, Any]:
        image, network, proxy = await asyncio.gather(
            self.podman(["image", "exists", IMAGE], 30, None),
            self.podman(["network", "exists", self.config.network], 30, None),
            self.podman(
                [
                    "container",
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    PROXY_CONTAINER,
                ],
                30,
                None,
            ),
        )
        problems = []
        if image[0] != 0:
            problems.append(f"image {IMAGE} is missing (uv run hostctl sandbox-setup)")
        if network[0] != 0:
            problems.append(
                f"network {self.config.network} is missing (uv run hostctl egress-setup)"
            )
        if proxy[0] != 0 or proxy[1].strip() != "true":
            problems.append(
                "the egress proxy isn't running (systemctl --user status egress-proxy)"
            )
        if not self.config.web_ips:
            problems.append(
                f"egress.toml has no {WEB_PROFILE} profile, so no workspace's runs can "
                "reach the web"
            )
        return {"problems": problems}

    def prune(self) -> None:
        cutoff = self.now() - RESULT_KEEP
        for run_id in [
            i
            for i, j in self._jobs.items()
            if j.finished is not None and j.finished < cutoff
        ]:
            del self._jobs[run_id]

    async def wait(self, run_id: str, job: Job) -> dict[str, Any]:
        """The run's result if it finishes within WAIT, else a note that it's still going.
        Shielded, so giving up on the wait leaves the run alone."""
        try:
            result = await asyncio.wait_for(asyncio.shield(job.task), WAIT)
        except TimeoutError:
            return {
                "run_id": run_id,
                "running": True,
                "seconds": round(self.now() - job.started, 1),
            }
        return {**result, "run_id": run_id}

    async def start(
        self, workspace: str, work: Coroutine[Any, Any, dict[str, Any]]
    ) -> dict[str, Any]:
        """Start `work` as a run of `workspace`'s, and wait for it as op_wait does."""
        self.prune()
        run_id = f"r-{secrets.token_hex(4)}"
        job = Job(workspace, asyncio.create_task(work), self.now())
        job.task.add_done_callback(lambda _: setattr(job, "finished", self.now()))
        self._jobs[run_id] = job
        return await self.wait(run_id, job)

    async def op_run(
        self,
        scope: dict[str, Any],
        language: str,
        code: str,
        timeout: int = DEFAULT_TIMEOUT,
        attachments: Any = None,
        attachments_known: bool = False,
        **newer: Any,
    ) -> dict[str, Any]:
        """Run a script. `attachments` are the chat's files, [{title, file}] (from the
        run-code skill, which looks them up), copied as text into /work/attachments first;
        with `attachments_known`, the lookup was whole, so copies of files no longer
        attached go. Arguments a newer skill sends that this runner doesn't know are
        ignored, so the skill and the runner can be updated in either order."""
        if newer:
            log.info("run: ignoring arguments %s", ", ".join(sorted(newer)))
        if language not in LANGUAGES:
            raise SandboxError(f"language must be one of: {', '.join(LANGUAGES)}")
        if not isinstance(code, str) or not code.strip():
            raise SandboxError("code is empty")
        timeout = max(1, min(int(timeout), MAX_TIMEOUT))
        s = self.scope(scope)
        return await self.start(
            s.workspace,
            self.execute(
                s, language, code, timeout, attachments, attachments_known is True
            ),
        )

    async def op_wait(self, scope: dict[str, Any], run_id: str) -> dict[str, Any]:
        workspace = self.scope(scope).workspace
        self.prune()
        if not (job := self._jobs.get(run_id)) or job.workspace != workspace:
            raise SandboxError(
                "no such run (it finished over an hour ago, or the runner restarted)"
            )
        return await self.wait(run_id, job)

    async def execute(
        self,
        scope: Scope,
        language: str,
        code: str,
        timeout: int,
        attachments: Any = None,
        known: bool = False,
    ) -> dict[str, Any]:
        """One run, start to finish; op_run keeps it as a task, which holds the workspace's
        lock throughout and one of the slots (an address) from writing the script until
        its container is removed. The chat's attachments are copied in first, so they
        aren't among the files the run changed."""
        script, interpreter = LANGUAGES[language]
        name = f"sandbox-{secrets.token_hex(6)}"
        run_dir = self.config.scripts / name
        async with self.lock(scope.workspace):
            try:
                copies, notes = await asyncio.to_thread(
                    sync_attachments, self.config.uploads, scope, attachments, known
                )
            except Exception:
                log.exception("attachments for %s", scope.workspace)
                copies, notes = [], ["the chat's attachments couldn't be copied"]
            before = await asyncio.to_thread(snapshot, scope)
            if before.total > workspace.WORKSPACE_MAX_BYTES:
                raise over_quota(before, "run code")
            access = self.access(scope)
            asking = (
                model_access.Models(
                    scope.workspace,
                    scope.thread,
                    access.daily_tokens,
                    self.config.model_log,
                    self.ask_model,
                )
                if access.models
                else None
            )
            async with (
                self.slot(web=access.web) as ip,
                contextlib.AsyncExitStack() as stack,
            ):
                try:
                    args = await asyncio.to_thread(
                        prepare,
                        self.config,
                        name,
                        scope,
                        run_dir,
                        script,
                        code,
                        ip,
                        access,
                    )
                    if asking is not None:
                        # The run's own socket, for as long as it runs; its folder goes
                        # once it's closed.
                        sockets = model_dir(self.config, name)
                        stack.callback(shutil.rmtree, sockets, True)
                        await stack.enter_async_context(
                            hostrpc.serving(asking, sockets / "sock")
                        )
                    started = self.now()
                    watch = asyncio.create_task(self.watch(scope, name))
                    try:
                        exit_code, out, err, timed_out = await self.podman(
                            args + [interpreter, f"/sandbox/{script}"], timeout, name
                        )
                    finally:
                        watch.cancel()
                    took = self.now() - started
                    (over,) = await asyncio.gather(watch, return_exceptions=True)
                    if isinstance(over, str) and over:
                        err = f"{err}\n[the sandbox stopped this run: {over}]".lstrip()
                    oom = False
                    if exit_code == 137 and not timed_out:
                        _, state, _, _ = await self.podman(
                            ["inspect", "--format", "{{.State.OOMKilled}}", name],
                            30,
                            None,
                        )
                        oom = state.strip() == "true"
                finally:
                    await self.podman(["rm", "-f", "--ignore", name], 60, None)
                    await asyncio.to_thread(shutil.rmtree, run_dir, True)
            # Outside the slot: walking the workspace needs no address.
            after = await asyncio.to_thread(snapshot, scope)
            published = await asyncio.to_thread(
                self.pages.page_changes, scope, public_changes(before, after)
            )
            changed = sorted(
                p for p, sig in after.files.items() if before.files.get(p) != sig
            )
            edited = [m[1] for p in changed if (m := APP_DATA_RE.fullmatch(p))]
            if edited:
                await asyncio.to_thread(self.rerender_apps, scope, edited)
        if edited:
            await self.app_changed()
        log.info(
            "run workspace=%s thread=%s lang=%s exit=%s timed_out=%s oom=%s %.1fs",
            scope.workspace,
            scope.thread,
            language,
            exit_code,
            timed_out,
            oom,
            took,
        )
        return {
            "exit_code": exit_code,
            "timed_out": timed_out,
            "oom_killed": oom,
            "timeout": timeout,
            "seconds": round(took, 1),
            "stdout": out,
            "stderr": err,
            "changed": changed[:LIST_MAX],
            "changed_more": max(0, len(changed) - LIST_MAX),
            "published": published,
            "warning": (
                f"this workspace's sandbox uses {after.total >> 20} MB of its {workspace.WORKSPACE_MAX_BYTES >> 20} MB; "
                "delete what isn't needed"
            )
            if after.total > workspace.WORKSPACE_WARN_BYTES
            else None,
            "attachments": copies,
            "attachment_notes": notes,
            "web": access.web,
            "models": {"tokens_left": await asyncio.to_thread(asking.left)}
            if asking is not None
            else None,
        }

    async def watch(self, scope: Scope, name: str) -> str:
        """While a run goes, look at the workspace's use every WATCH_SECONDS and kill the
        run if it fills the disk; what it went over, or "" if it was stopped first."""
        while True:
            await asyncio.sleep(WATCH_SECONDS)
            usage = await asyncio.to_thread(snapshot, scope)
            most = workspace.WORKSPACE_MAX_BYTES + workspace.RUN_SLACK
            if usage.total > most:
                over = f"the workspace went over {most >> 20} MB"
            elif usage.count > workspace.MAX_FILES:
                over = (
                    f"the workspace went over {workspace.MAX_FILES} files and folders"
                )
            else:
                continue
            log.warning("killing run %s in %s: %s", name, scope.workspace, over)
            await self.podman(["kill", name], 30, None)
            return over

    # --- site builds ---

    async def op_build_site(
        self, scope: dict[str, Any], path: str, slug: str = ""
    ) -> dict[str, Any]:
        """Build the Zola site in `path` (a folder in the workspace's own /project,
        /shared/<workspace> or /work) into /public/<slug>, and publish it. The slug is the
        folder's name unless given. A build that outlasts the call goes on, like a run."""
        s = self.scope(scope)
        mount, _, _ = split(s, path)
        if mount == "/public":
            raise SandboxError(
                "build a site from its source in /project or /shared, not from /public"
            )
        slug = slug or Path(path.strip().rstrip("/")).name
        if not SLUG_RE.fullmatch(slug):
            raise SandboxError(
                f"'{slug}' isn't a page name; give slug: 1-63 lowercase letters, digits "
                "or hyphens"
            )
        self.idle(s.workspace)
        return await self.start(
            s.workspace, self.build(s, path.strip().rstrip("/"), slug)
        )

    async def build(self, scope: Scope, path: str, slug: str) -> dict[str, Any]:
        """One site build, under the workspace's lock: the helper in a container with no
        network, then the output copied into /public/<slug> and synced."""
        name = f"sandbox-{secrets.token_hex(6)}"
        run_dir = self.config.scripts / name
        async with self.lock(scope.workspace):
            usage = await asyncio.to_thread(snapshot, scope)
            if usage.total > workspace.WORKSPACE_MAX_BYTES:
                raise over_quota(usage, "build a site")
            source = resolve(scope, path)
            if not (source / "zola.toml").is_file():
                raise SandboxError(
                    f"'{path}' has no zola.toml, so it isn't a Zola site"
                )
            url = self.public_url(scope.workspace, slug)
            try:
                args = await asyncio.to_thread(
                    prepare_build, self.config, name, scope, run_dir
                )
                async with self.slot():
                    exit_code, out, err, timed_out = await self.podman(
                        [*args, "python", "/sandbox/sitebuild.py", path, url],
                        BUILD_TIMEOUT,
                        name,
                    )
                if timed_out:
                    raise SandboxError(
                        f"the build took over {BUILD_TIMEOUT} s and was stopped"
                    )
                if exit_code != 0:
                    raise SandboxError(
                        f"the site didn't build: {(err or out).strip()[-2000:]}"
                    )
                files = await asyncio.to_thread(
                    self.pages.stage_files, scope, run_dir / "out" / "site", slug
                )
            finally:
                await self.podman(["rm", "-f", "--ignore", name], 60, None)
                await asyncio.to_thread(shutil.rmtree, run_dir, True)
            published = await asyncio.to_thread(self.pages.page_changes, scope, {slug})
        log.info(
            "built %s from %s for %s: %d files", slug, path, scope.workspace, files
        )
        return {
            "slug": slug,
            "url": f"{url}/",
            "files": files,
            "zola": (out + err).strip()[-500:],  # zola reports on stderr
            "published": published,
        }

    # --- files ---

    async def op_write(
        self, scope: dict[str, Any], path: str, content: str = "", delete: bool = False
    ) -> dict[str, Any]:
        """Write a text file, or delete a file or folder (always allowed, so a workspace over
        its limit can get back under). Deleting exactly one of the workspace's mounts empties
        it."""
        s = self.scope(scope)
        mount = split(s, path)[0]
        async with self.exclusive(s.workspace):
            result = await self.write(s, path, content, delete)
            if mount == "/public" and (target := split(s, path)[2]) != s.public:
                result["published"] = await asyncio.to_thread(
                    self.pages.page_changes, s, {target.relative_to(s.public).parts[0]}
                )
        return result

    async def write(
        self, s: Scope, path: str, content: str, delete: bool
    ) -> dict[str, Any]:
        """op_write's work, under the workspace's lock."""
        if delete:
            return await asyncio.to_thread(self.delete, s, path)
        data = (content or "").encode()
        if len(data) > pages.WRITE_BYTES:
            raise SandboxError(
                f"content is {len(data)} bytes; the limit is {pages.WRITE_BYTES}"
            )
        usage = await asyncio.to_thread(snapshot, s)
        if usage.total + len(data) > workspace.WORKSPACE_MAX_BYTES:
            raise over_quota(usage, "write files")
        target = resolve(s, path)
        await asyncio.to_thread(write_regular, target, data, path)
        return {"path": path.strip(), "bytes": len(data)}

    def delete(self, scope: Scope, path: str) -> dict[str, Any]:
        """Only the parent is resolved: a symlink is removed itself, never what it points to."""
        mount, root, target = split(scope, path)
        if target == root:
            if path.strip() != mount:
                raise SandboxError(
                    "give the path of the file or folder to delete, or one of "
                    f"{', '.join(scope.roots)} to empty it"
                )
            for child in list(root.iterdir()):
                remove_path(child)
            return {"path": mount, "emptied": True}
        parent = target.parent.resolve()
        if not parent.is_relative_to(root.resolve()):
            raise SandboxError(f"'{path}' points outside {mount}")
        target = parent / target.name
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            raise SandboxError(f"there's no '{path}'") from None
        remove_path(target)
        return {"path": path.strip(), "folder": stat.S_ISDIR(st.st_mode)}

    # --- pages (sandbox.pages) ---

    def public_url(self, workspace: str, path: str = "") -> str:
        """Where `path` in a workspace's /public is on the workspace pages site."""
        return self.pages.public_url(workspace, path)

    async def op_publish(
        self,
        scope: dict[str, Any],
        slug: str = "",
        path: str = "",
        remove: bool = False,
    ) -> dict[str, Any]:
        """A page's address and link card. /public is the workspace's pages, live as they're
        written, so nothing needs publishing; with `path` outside /public, that file or
        folder is first copied to /public/<slug> (an HTML file as its index.html), and with
        `remove`, /public's entry for the page is deleted. Without a slug or path, the
        workspace's pages."""
        s = self.scope(scope)
        path = (path or "").strip()
        outside = bool(path) and split(s, path)[0] != "/public"
        if (outside or (slug and not path)) and not (
            isinstance(slug, str) and SLUG_RE.fullmatch(slug)
        ):
            raise SandboxError(
                "slug must be 1-63 lowercase letters, digits or hyphens, not starting "
                "or ending with a hyphen, e.g. 'trip-plan'"
            )
        async with self.exclusive(s.workspace):
            if outside:
                await asyncio.to_thread(self.pages.stage, s, path, slug)
                name = slug
            elif path:
                target = split(s, path)[2]
                if target == s.public:
                    raise SandboxError(
                        "give a page in /public, e.g. /public/trip-plan, not all of it"
                    )
                name = target.relative_to(s.public).parts[0]
            elif slug:
                entries = self.pages.public_entries(s.public, slug)
                name = entries[0].name if entries else slug
            else:
                return await asyncio.to_thread(self.pages.listing, s)
            item = s.public / name
            if remove:
                if path and not outside:
                    # The entry the path names, or else its file (/public/notes for
                    # notes.html), never others that share a stem (notes.css, v1.3).
                    entries = (
                        [item]
                        if os.path.lexists(item)
                        else self.pages.public_entries(s.public, name)
                    )
                else:
                    entries = self.pages.public_entries(s.public, slug) or (
                        [item] if os.path.lexists(item) else []
                    )
                if not entries:
                    raise SandboxError(f"there's no page '{name}' in /public")
                for e in entries:
                    url = self.pages.page_url(
                        s.workspace, e
                    )  # before: a folder's ends in /
                    await asyncio.to_thread(remove_path, e)
                    # Its link card too, which shows the page's title and description.
                    chatimage.card.remove(self.config.site_dir, url)
                return {"slug": name, "removed": True}
            if not (item.is_dir() or item.is_file()) or item.is_symlink():
                raise SandboxError(
                    f"there's nothing at /public/{name} to publish; give the path of "
                    "the file or folder to publish"
                )
            return await asyncio.to_thread(self.pages.page_info, s.workspace, item)

    async def op_show_image(
        self, scope: dict[str, Any], path: str = "", alt: str = ""
    ) -> dict[str, Any]:
        """An image in the caller's folders, put on the pages site for the chat: its
        address, size and the Markdown line that shows it (`image`)."""
        s = self.scope(scope)
        if not isinstance(path, str) or not path.strip():
            raise SandboxError("give the image's path, e.g. /work/chart.png")
        if not isinstance(alt, str):
            raise SandboxError("alt must be text")
        async with self.exclusive(s.workspace):
            data = await asyncio.to_thread(self.pages.read_image, s, path)
            return await asyncio.to_thread(
                self.pages.put_image, s.workspace, data, path.strip(), alt
            )

    # --- apps (sandbox.apps) ---

    async def op_app(
        self,
        scope: dict[str, Any],
        action: str = "list",
        name: str = "",
        template: str = "list",
        title: str = "",
        op: str = "",
        args: Any = None,
    ) -> dict[str, Any]:
        """An app of the workspace's: `create` one from a template, `do` one of its ops,
        `show` it, `list` them, or `delete` one. Every change re-renders its page in
        /public/apps/<name>/ and moves its live card on (sandbox.appsweb)."""
        if action not in APP_ACTIONS:
            raise SandboxError(f"action must be one of: {', '.join(APP_ACTIONS)}")
        s = self.scope(scope)
        if s.gateway:
            raise SandboxError("a gateway client has no chat to show an app in")
        if action != "list" and not (isinstance(name, str) and SLUG_RE.fullmatch(name)):
            raise SandboxError(
                "name must be 1-63 lowercase letters, digits or hyphens, e.g. 'groceries'"
            )
        async with self.exclusive(s.workspace):
            if action == "list":
                return {"apps": await asyncio.to_thread(self.list_apps, s)}
            if action == "show":
                return await asyncio.to_thread(self.show_app, s, name)
            if action == "delete":
                await asyncio.to_thread(self.delete_app, s, name)
                result: dict[str, Any] = {"name": name, "deleted": True}
            elif action == "create":
                data, did = await asyncio.to_thread(
                    self.create_app, s, name, template, title, args
                )
                result = self.app_reply(s, name, data, did)
            else:
                data, did, _ = await asyncio.to_thread(
                    self.change_app, s, name, op, args
                )
                result = self.app_reply(s, name, data, did)
        await self.app_changed()
        return result

    async def app_changed(self) -> None:
        async with self.apps_changed:
            self.apps_changed.notify_all()

    def read_app(self, s: Scope, name: str) -> dict[str, Any]:
        """The app's data, read without following a symlink and checked by its template."""
        raw = safefs.read_regular(
            s.roots["/project"], ("apps", name, "data.json"), APP_DATA_BYTES + 1
        )
        if raw is None:
            raise NoSuchApp(f"there's no app '{name}' (app list shows them)")
        if len(raw) > APP_DATA_BYTES:
            raise SandboxError(f"{name}'s data is over {APP_DATA_BYTES >> 20} MB")
        try:
            data = json.loads(raw)
            return app_templates.of(data).validate(data)
        except ValueError as e:  # AppError is one
            raise SandboxError(f"{name}'s data can't be used: {e}") from None

    def save_app(
        self, s: Scope, name: str, data: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        """Write the app's data, one version on, and its page, under a new write-back
        token; the data as saved, and the token."""
        data = {**data, "version": data["version"] + 1}
        try:
            with safefs.folder(s.roots["/project"], ("apps", name), make=True) as d:
                safefs.replace(d, "data.json", json.dumps(data, indent=1).encode())
            token = self.app_tokens.rotate(s.workspace, name)
            page = app_templates.render(
                data, token, f"/_apps/{quote(s.workspace)}/{quote(name)}/ops"
            )
            with safefs.folder(s.public, ("apps", name), make=True) as d:
                safefs.replace(d, "index.html", page.encode())
        except OSError as e:
            raise SandboxError(f"couldn't save {name}: {e}") from None
        return data, token

    def app_reply(
        self, s: Scope, name: str, data: dict[str, Any], did: str = ""
    ) -> dict[str, Any]:
        kind = app_templates.of(data)
        live = (
            f"{self.config.site_url.rstrip('/')}/_live/apps/"
            f"{quote(s.workspace)}/{quote(name)}"
        )
        return {
            "name": name,
            "template": kind.NAME,
            "title": data["title"],
            "summary": kind.summary(data),
            "did": did,
            "version": data["version"],
            "page": self.public_url(s.workspace, f"apps/{quote(name)}/"),
            "card": chatimage.linked_image(data["title"], live + ".png", live),
        }

    def create_app(
        self, s: Scope, name: str, template: str, title: str, args: Any
    ) -> tuple[dict[str, Any], str]:
        """Make the app; its data as saved, and what was done."""
        project = s.roots["/project"]
        if safefs.read_regular(project, ("apps", name, "data.json"), 1) is not None:
            raise SandboxError(f"there's an app '{name}' already; change it with do")
        try:
            kind = app_templates.template(template)
            data = kind.new(title or name.replace("-", " ").capitalize())
            did = "made it"
            if isinstance(args, dict) and (args.get("items") or args.get("item")):
                data, did = kind.apply(data, "add", args)
                did = f"made it and {did}"
        except ValueError as e:
            raise SandboxError(str(e)) from None
        return self.save_app(s, name, data)[0], did

    def change_app(
        self, s: Scope, name: str, op: str, args: Any, token: str | None = None
    ) -> tuple[dict[str, Any], str, str]:
        """Apply one of the app's ops and save it; its data as saved, what was done, and
        the page's new token. With `token` (a page's), only when it's the app's current
        one (Tokens.check)."""
        data = self.read_app(s, name)
        if token is not None:
            self.app_tokens.check(s.workspace, name, token)
        try:
            data, did = app_templates.of(data).apply(data, op, args)
        except ValueError as e:
            raise SandboxError(str(e)) from None
        saved, token = self.save_app(s, name, data)
        return saved, did, token

    def rerender_apps(self, s: Scope, names: list[str]) -> None:
        """Render the pages of the apps whose data a run changed, so they show it."""
        for name in names:
            try:
                self.save_app(s, name, self.read_app(s, name))
            except SandboxError as e:  # the run's to fix; show and do say so
                log.info("app %s/%s after a run: %s", s.workspace, name, e)

    def show_app(self, s: Scope, name: str) -> dict[str, Any]:
        data = self.read_app(s, name)
        if not (s.public / "apps" / name / "index.html").is_file():
            data, _ = self.save_app(s, name, data)  # a page a run removed comes back
        return self.app_reply(s, name, data)

    def list_apps(self, s: Scope) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        try:
            entries = sorted(
                os.scandir(s.roots["/project"] / "apps"), key=lambda e: e.name
            )
        except OSError:
            return found
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False) or not SLUG_RE.fullmatch(
                entry.name
            ):
                continue
            try:
                found.append(
                    self.app_reply(s, entry.name, self.read_app(s, entry.name))
                )
            except SandboxError as e:
                found.append({"name": entry.name, "error": str(e)})
        return found

    def delete_app(self, s: Scope, name: str) -> None:
        folder = s.roots["/project"] / "apps" / name
        if not os.path.lexists(folder):
            raise NoSuchApp(f"there's no app '{name}' (app list shows them)")
        remove_path(folder)
        remove_path(s.public / "apps" / name)
        self.app_tokens.remove(s.workspace, name)

    # --- server ---

    async def cleanup(self) -> None:
        """Remove what an earlier runner left mid-run: its containers and script folders."""
        await self.podman(["rm", "-f", "--filter", f"label={LABEL}"], 120, None)
        await asyncio.to_thread(shutil.rmtree, self.config.scripts, True)

    async def gc_loop(self) -> None:
        while True:
            if removed := await asyncio.to_thread(self.gc):
                log.info("removed idle threads' /work: %s", ", ".join(removed))
            await asyncio.sleep(3600)


async def serve(config: Config, stop: asyncio.Event | None = None) -> None:
    """Serve the runner on its socket, and the apps server (sandbox.appsweb) on its port,
    until `stop` is set, or without one until SIGTERM."""
    runner = Runner(config)
    await runner.cleanup()
    config.root.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    on_sigterm = stop is None
    if stop is None:
        stop = asyncio.Event()
        loop.add_signal_handler(signal.SIGTERM, stop.set)
    gc = asyncio.create_task(runner.gc_loop())
    web = (
        await appsweb.AppsWeb(runner).serve(config.apps_port)
        if config.apps_port
        else None
    )
    try:
        await hostrpc.serve(runner, config.socket, limit=LIMIT, stop=stop)
    finally:
        gc.cancel()
        if web is not None:
            web.close()
        if on_sigterm:
            loop.remove_signal_handler(signal.SIGTERM)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(serve(Config.from_env()))
