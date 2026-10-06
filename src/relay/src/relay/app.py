"""relay: the Nilson relay's HTTP API, served by uvicorn on the host and reached over the
tailnet through `tailscale serve` (https). Every route but /health needs the relay's token
as a bearer token; see the README's "Nilson relay" for the routes.

Config (environment; the unit reads host.env, then ~/.config/everythingllm/relay.env, which
holds the secrets, outside the repo and the AnythingLLM container's reach):
  ANYTHINGLLM_API_KEY  developer API key the relay calls AnythingLLM with (required)
  RELAY_TOKEN          the token Nilson sends the relay (required)
  ANYTHINGLLM_URL      AnythingLLM's base URL (default http://127.0.0.1:3001)
  DATABASE_PATH        the SQLite file (default ~/.local/share/everythingllm/relay/relay.db)
  NTFY_URL, NTFY_TOKEN the ntfy topic told about finished runs, and its token; no
                       notifications without NTFY_URL
  RUN_RETENTION_DAYS   how long finished runs are kept (default 7)
  RELAY_HOST, RELAY_PORT  where to listen (default 127.0.0.1:8446)
"""

import contextlib
import hmac
import logging
import os
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from relay import notify, upstream
from relay.runs import Answer, Busy, Notify, Relay
from relay.store import STATUSES, Store, public

log = logging.getLogger("relay")

MODES = ("query", "chat")
DATABASE = Path("~/.local/share/everythingllm/relay/relay.db").expanduser()


@dataclass
class Config:
    api_key: str
    token: str
    anythingllm_url: str = "http://127.0.0.1:3001"
    database: Path = DATABASE
    ntfy_url: str = ""
    ntfy_token: str = ""
    retention_days: float = 7
    host: str = "127.0.0.1"
    port: int = 8446

    @classmethod
    def from_env(cls) -> "Config":
        get = os.environ.get
        missing = [k for k in ("ANYTHINGLLM_API_KEY", "RELAY_TOKEN") if not get(k)]
        if missing:
            raise SystemExit(
                f"relay: set {' and '.join(missing)} in ~/.config/everythingllm/relay.env"
            )
        return cls(
            api_key=get("ANYTHINGLLM_API_KEY", ""),
            token=get("RELAY_TOKEN", ""),
            anythingllm_url=get("ANYTHINGLLM_URL") or cls.anythingllm_url,
            database=Path(get("DATABASE_PATH") or DATABASE).expanduser(),
            ntfy_url=get("NTFY_URL", ""),
            ntfy_token=get("NTFY_TOKEN", ""),
            retention_days=float(get("RUN_RETENTION_DAYS") or cls.retention_days),
            host=get("RELAY_HOST") or cls.host,
            port=int(get("RELAY_PORT") or cls.port),
        )


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


class RequireToken:
    """Answers 401 to any HTTP request but /health without `Authorization: Bearer <token>`.
    Plain ASGI, so streamed responses pass through untouched."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] != "/health":
            given = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(given, self.expected):
                await error(401, "A valid relay token is required.")(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


def create_app(
    config: Config,
    answer: Answer | None = None,
    notify_finished: Notify | None = None,
) -> Starlette:
    """The API, with its relay at `app.state.relay`. The relay asks AnythingLLM and ntfy
    through one HTTP client; a test passes its own `answer` and `notify_finished` instead.
    The lifespan starts the relay and closes the client and the store."""
    client = httpx.AsyncClient()
    store = Store(config.database)
    if answer is None:
        answer = partial(
            upstream.answer, client, config.anythingllm_url, config.api_key
        )
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
        return JSONResponse({"ok": True})

    async def create_run(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return error(400, "The body must be JSON.")
        if not isinstance(body, dict):
            return error(400, "The body must be a JSON object.")
        fields = {}
        for key in ("workspace", "thread", "message", "clientId"):
            value = body.get(key)
            if not isinstance(value, str) or not value.strip():
                return error(400, f"'{key}' must be a non-empty string.")
            fields[key] = value
        mode = body.get("mode", "chat")
        if mode not in MODES:
            return error(400, "'mode' must be 'query' or 'chat'.")
        try:
            run, created = await relay.start(
                fields["clientId"],
                fields["workspace"],
                fields["thread"],
                fields["message"],
                mode,
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
        Route("/runs", create_run, methods=["POST"]),
        Route("/runs", list_runs, methods=["GET"]),
        Route("/runs/{id}", get_run, methods=["GET"]),
        Route("/runs/{id}/events", events, methods=["GET"]),
        Route("/runs/{id}/cancel", cancel, methods=["POST"]),
    ]
    app = Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[Middleware(RequireToken, token=config.token)],
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
        create_app(config),
        host=config.host,
        port=config.port,
        log_level="info",
        proxy_headers=True,
        timeout_graceful_shutdown=5,
    )
