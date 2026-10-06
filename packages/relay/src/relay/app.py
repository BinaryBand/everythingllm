"""relay: the Nilson relay's HTTP API, served by uvicorn in its service container and reached
over the tailnet through `tailscale serve` (https). Every route but /health needs the
relay's token as a bearer token; see the README's "Nilson relay" for the routes. Under
/api/v1/ it is AnythingLLM's developer API (`relay.proxy`), so the token serves as any
client's API key.

Config (environment; the container gets host.env, then ~/.config/everythingllm/relay.env,
which holds the secrets, outside the repo and the AnythingLLM container's reach, then what
host/quadlet/relay.container.in sets itself):
  ANYTHINGLLM_API_KEY  developer API key the relay calls AnythingLLM with (required)
  RELAY_TOKEN          the token Nilson sends the relay (required)
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
from relay.proxy import PREFIX, Proxy
from relay.runs import Answer, Busy, Notify, Relay
from relay.store import STATUSES, Store, public

log = logging.getLogger("relay")

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
    forwarded_allow_ips: str = "127.0.0.1"

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
            forwarded_allow_ips=get("FORWARDED_ALLOW_IPS") or cls.forwarded_allow_ips,
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
    client: httpx.AsyncClient | None = None,
) -> Starlette:
    """The API, with its relay at `app.state.relay`. The relay, the proxy and ntfy share one
    HTTP client; a test passes its own `answer`, `notify_finished` and `client` instead.
    The lifespan starts the relay and closes the client and the store."""
    client = client or httpx.AsyncClient()
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
                fields["clientId"], fields["workspace"], fields["thread"], chat
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
        Route(
            PREFIX + "{path:path}",
            Proxy(client, config.anythingllm_url, config.api_key),
        ),
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
        # tailscale serve passes on who asked, and over https: believed only from the peer
        # it reaches the relay through (FORWARDED_ALLOW_IPS above).
        proxy_headers=True,
        forwarded_allow_ips=config.forwarded_allow_ips,
        timeout_graceful_shutdown=5,
    )
