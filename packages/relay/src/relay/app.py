"""relay: the Nilson relay's HTTP API, served by uvicorn in its service container and
reached over the tailnet at `/everythingllm/` on AnythingLLM's own port, through `tailscale
serve` (https), which strips that prefix; the routes answer with or without it. Every route
but /health needs an AnythingLLM developer API key as a bearer token (`relay.auth`), the one
the client gives AnythingLLM itself; see the README's "Nilson relay" for the routes. A
request from anywhere but loopback or the relay's own address, where the container's
published port delivers from, is refused with a 403 (`LocalPeers`): in the container,
that's another container on egress-net.

Config (environment; the container gets host.env, then ~/.config/everythingllm/relay.env,
which holds the ntfy settings, outside the repo and the AnythingLLM container's reach, then
what host/quadlet/relay.container.in sets itself):
  ANYTHINGLLM_URL      AnythingLLM's base URL (default http://127.0.0.1:3001; the container
                       has https://<PUBLIC_HOST>:3001, through the egress proxy)
  DATABASE_PATH        the SQLite file (default ~/.local/share/everythingllm/relay/relay.db)
  NTFY_URL, NTFY_TOKEN the ntfy topic told about finished runs, and its token; no
                       notifications without NTFY_URL
  RUN_RETENTION_DAYS   how long finished runs are kept (default 7)
  RELAY_HOST, RELAY_PORT  where to listen (default 127.0.0.1:8446; the container listens
                       on 0.0.0.0, published on the host's 127.0.0.1:8446)
  FORWARDED_ALLOW_IPS  the peers whose X-Forwarded-For and X-Forwarded-Proto uvicorn
                       believes, comma-separated (default 127.0.0.1, where tailscale serve
                       connects from on the host). Through the container's published port
                       every connection arrives from the container's own address, so its
                       template sets that address.
  HTTPS_PROXY          the egress proxy, which httpx goes out through (set in the container)
"""

import contextlib
import logging
import os
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import hostrpc
import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from relay import notify, upstream
from relay.auth import KeyCheck, RequireKey
from relay.runs import Answer, Busy, Notify, Relay
from relay.store import STATUSES, Store, public

log = logging.getLogger("relay")

DATABASE = Path("~/.local/share/everythingllm/relay/relay.db").expanduser()
# Where the relay is mounted beside AnythingLLM on the tailnet.
PREFIX = "/everythingllm"
# What GET /health tells a client, so it knows the relay is there: AnythingLLM answers an
# unknown path with its web app's page and a 200.
HEALTH = {"ok": True, "service": "everythingllm", "features": ["runs"]}


@dataclass
class Config:
    anythingllm_url: str = "http://127.0.0.1:3001"
    database: Path = DATABASE
    ntfy_url: str = ""
    ntfy_token: str = ""
    retention_days: float = 7
    host: str = "127.0.0.1"
    port: int = 8446
    forwarded_allow_ips: str = "127.0.0.1"

    @classmethod
    def from_env(cls) -> "Config":
        get = os.environ.get
        return cls(
            anythingllm_url=get("ANYTHINGLLM_URL") or cls.anythingllm_url,
            database=Path(get("DATABASE_PATH") or DATABASE).expanduser(),
            ntfy_url=get("NTFY_URL", ""),
            ntfy_token=get("NTFY_TOKEN", ""),
            retention_days=float(get("RUN_RETENTION_DAYS") or cls.retention_days),
            host=get("RELAY_HOST") or cls.host,
            port=int(get("RELAY_PORT") or cls.port),
            forwarded_allow_ips=get("FORWARDED_ALLOW_IPS") or cls.forwarded_allow_ips,
        )


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


class StripPrefix:
    """Takes the routes under `prefix` as they are at the root: `tailscale serve` strips its
    mount path, and a proxy that doesn't reaches the same routes."""

    def __init__(self, app: ASGIApp, prefix: str) -> None:
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and (scope["path"] + "/").startswith(
            self.prefix + "/"
        ):
            scope = {**scope, "path": scope["path"][len(self.prefix) :] or "/"}
        await self.app(scope, receive, send)


class LocalPeers:
    """Refuses a request whose connection comes from anywhere but loopback or the relay's
    own address (hostrpc.local_peer), then has uvicorn's proxy headers believe tailscale
    serve's X-Forwarded-For and X-Forwarded-Proto from `forwarded_allow_ips`. uvicorn's own
    (`proxy_headers=True`) would go first and put the forwarded client in the place of the
    peer judged here."""

    def __init__(self, app: ASGIApp, forwarded_allow_ips: str) -> None:
        # uvicorn types an ASGI app more narrowly than starlette does.
        headers: Any = ProxyHeadersMiddleware
        self.app: ASGIApp = headers(app, trusted_hosts=forwarded_allow_ips)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not hostrpc.local_peer(
            scope.get("client"), scope.get("server")
        ):
            return await error(403, "Not from here.")(scope, receive, send)
        await self.app(scope, receive, send)


def create_app(
    config: Config,
    answer: Answer | None = None,
    notify_finished: Notify | None = None,
    client: httpx.AsyncClient | None = None,
) -> Starlette:
    """The API, with its relay at `app.state.relay`. The relay, the key check and ntfy share
    one HTTP client; a test passes its own `answer`, `notify_finished` and `client` instead.
    The lifespan starts the relay and closes the client and the store."""
    client = client or httpx.AsyncClient()
    store = Store(config.database)
    if answer is None:
        answer = partial(upstream.answer, client, config.anythingllm_url)
    if notify_finished is None and config.ntfy_url:
        notify_finished = notify.publisher(client, config.ntfy_url, config.ntfy_token)
    relay = Relay(store, answer, notify_finished, config.retention_days)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        try:
            async with client, relay:
                yield
        finally:
            store.close()

    async def health(_request: Request) -> Response:
        return JSONResponse(HEALTH)

    async def create_run(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return error(400, "The body must be JSON.")
        if not isinstance(body, dict):
            return error(400, "The body must be a JSON object.")
        fields = {}
        for key in ("workspace", "thread", "clientId"):
            value = body.get(key)
            if not isinstance(value, str) or not value.strip():
                return error(400, f"'{key}' must be a non-empty string.")
            fields[key] = value
        chat = body.get("body")
        if chat is None and ("message" in body or "mode" in body):
            return error(400, "Send the stream-chat body as 'body'.")
        if not isinstance(chat, dict):
            return error(400, "'body' must be a JSON object.")
        message = chat.get("message")
        if (
            not (isinstance(message, str) and message.strip())
            and chat.get("reset") is not True
        ):
            return error(400, "'body' needs a non-empty 'message', or 'reset': true.")
        try:
            run, created = await relay.start(
                fields["clientId"],
                fields["workspace"],
                fields["thread"],
                chat,
                request.state.api_key,
            )
        except Busy:
            return error(409, "That thread already has an answer running.")
        return JSONResponse(run, status_code=201 if created else 200)

    async def list_runs(request: Request) -> Response:
        status = request.query_params.get("status")
        if status is not None and status not in STATUSES:
            return error(400, f"'status' must be one of {', '.join(STATUSES)}.")
        return JSONResponse([public(r) for r in relay.store.runs(status)])

    async def get_run(request: Request) -> Response:
        row = relay.store.get(request.path_params["id"])
        return JSONResponse(public(row)) if row else error(404, "No such run.")

    async def events(request: Request) -> Response:
        run_id = request.path_params["id"]
        if relay.store.get(run_id) is None:
            return error(404, "No such run.")
        try:
            after = max(0, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            after = 0
        return StreamingResponse(
            relay.follow(run_id, after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def cancel(request: Request) -> Response:
        run = await relay.cancel(request.path_params["id"])
        return JSONResponse(run) if run else error(404, "No such run.")

    routes = [
        Route("/health", health),
        Route("/v1/runs", create_run, methods=["POST"]),
        Route("/v1/runs", list_runs, methods=["GET"]),
        Route("/v1/runs/{id}", get_run, methods=["GET"]),
        Route("/v1/runs/{id}/events", events, methods=["GET"]),
        Route("/v1/runs/{id}/cancel", cancel, methods=["POST"]),
    ]
    app = Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[
            Middleware(StripPrefix, prefix=PREFIX),
            Middleware(RequireKey, check=KeyCheck(client, config.anythingllm_url)),
        ],
    )
    app.state.relay = relay
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # httpx logs every request's URL at INFO, and the ntfy topic's URL is a secret.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = Config.from_env()
    uvicorn.run(
        # tailscale serve passes on who asked, and over https: believed only from the peer
        # it reaches the relay through (FORWARDED_ALLOW_IPS above), by LocalPeers.
        LocalPeers(create_app(config), config.forwarded_allow_ips),
        host=config.host,
        port=config.port,
        log_level="info",
        proxy_headers=False,
        timeout_graceful_shutdown=5,
    )
