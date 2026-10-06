"""gateway: the runners' MCP tools over streamable HTTP, for MCP clients other than
AnythingLLM (Claude Code, …), served by uvicorn on the host and reached over the tailnet
through `tailscale serve` (https). See the README's "MCP gateway".

Its tools are the fronts' own: the read tools of sites, podcasts and audit (each front's
`tool.registered`, so the same schemas and docstrings AnythingLLM sees), and the agents
ops in gateway.agents. A front's skills (the ops that write or act) aren't tools, so they
aren't here. Each call goes to its runner's socket as the host sees it.

Every path but /health needs `Authorization: Bearer <token>`, one token per client. Each
tool call is logged with the client's name and the tool's, never its arguments.

Config (environment; the unit reads host.env, then ~/.config/everythingllm/gateway.env,
which holds the tokens, outside the repo and the AnythingLLM container's reach):
  GATEWAY_TOKEN_<NAME>    a client's token; the client is <name>, lowercase, _ as -
                          (at least one)
  PUBLIC_HOST             the tailnet name, whose Host header is allowed (from host.env)
  GATEWAY_HOST, GATEWAY_PORT  where to listen (default 127.0.0.1:8452; tailnet https is
                          the same port)
  <FRONT>_SOCKET          a runner's socket (default storage/everythingllm/<front>/runner.sock,
                          storage as host.env's ANYTHINGLLM_STORAGE has it)
"""

import hmac
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any

import audit.server
import hostrpc
import podcasts.server
import sites.server
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from gateway import agents

log = logging.getLogger("gateway")

FRONTS = (sites.server, podcasts.server, audit.server, agents)

TOKEN_PREFIX = "GATEWAY_TOKEN_"

# Where RequireToken leaves the client's name for the call log.
CLIENT_KEY = "gateway.client"


@dataclass
class Config:
    clients: dict[str, str] = field(default_factory=dict)  # name -> token
    public_host: str = ""
    host: str = "127.0.0.1"
    port: int = 8452

    @classmethod
    def from_env(cls) -> "Config":
        get = os.environ.get
        clients = {
            k.removeprefix(TOKEN_PREFIX).lower().replace("_", "-"): v
            for k, v in os.environ.items()
            if k.startswith(TOKEN_PREFIX) and v
        }
        if not clients:
            raise SystemExit(
                f"gateway: set a {TOKEN_PREFIX}<NAME> in ~/.config/everythingllm/gateway.env"
            )
        return cls(
            clients=clients,
            public_host=get("PUBLIC_HOST", ""),
            host=get("GATEWAY_HOST") or cls.host,
            port=int(get("GATEWAY_PORT") or cls.port),
        )


def host_sockets() -> None:
    """Point each front at its runner's socket as the host sees it: the fronts' caller
    otherwise falls back to the container's storage path."""
    for front in FRONTS:
        env = front.skills.env
        os.environ.setdefault(env, str(hostrpc.socket_path(front.skills.folder, env)))


class RequireToken:
    """Answers 401 to any HTTP request but /health without one of the clients' bearer
    tokens, and leaves the client's name in the scope. Plain ASGI, so streamed responses
    and the app's lifespan pass through untouched."""

    def __init__(self, app: ASGIApp, clients: dict[str, str]) -> None:
        self.app = app
        self.expected = [
            (name, f"Bearer {token}".encode()) for name, token in clients.items()
        ]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] != "/health":
            given = dict(scope["headers"]).get(b"authorization", b"")
            # Compare against every token, so the time taken doesn't say which matched.
            matched = [n for n, e in self.expected if hmac.compare_digest(given, e)]
            if not matched:
                await JSONResponse(
                    {"error": "A valid gateway token is required."}, status_code=401
                )(scope, receive, send)
                return
            scope[CLIENT_KEY] = matched[0]
        await self.app(scope, receive, send)


async def log_calls(ctx: Any, call_next: Any) -> Any:
    """MCP middleware: a line for each tool call, naming the client and the tool."""
    if ctx.method == "tools/call":
        scope = getattr(ctx.request, "scope", None) or {}
        log.info(
            "%s called %s",
            scope.get(CLIENT_KEY, "?"),
            (ctx.params or {}).get("name", "?"),
        )
    return await call_next(ctx)


def build_mcp() -> MCPServer:
    mcp = MCPServer(
        "everythingllm",
        instructions=(
            "EverythingLLM's runners: the sites' entries and the news feeds, the "
            "podcasts, the system audit's checks, and delegations to AnythingLLM's own "
            "agents."
        ),
        middleware=[log_calls],
    )
    seen: dict[str, str] = {}
    for front in FRONTS:
        for fn in front.tool.registered:
            if fn.__name__ in seen:
                raise RuntimeError(
                    f"{front.__name__} and {seen[fn.__name__]} both have a tool "
                    f"{fn.__name__}"
                )
            seen[fn.__name__] = front.__name__
            mcp.add_tool(fn)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True})

    return mcp


def create_app(config: Config) -> ASGIApp:
    hosts = [f"127.0.0.1:{config.port}", f"localhost:{config.port}"]
    if config.public_host:
        hosts += [config.public_host, f"{config.public_host}:{config.port}"]
    app = build_mcp().streamable_http_app(
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts
        ),
    )
    return RequireToken(app, config.clients)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    config = Config.from_env()
    host_sockets()
    log.info("clients: %s", ", ".join(sorted(config.clients)))
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        log_level="info",
        proxy_headers=True,
        timeout_graceful_shutdown=5,
    )
