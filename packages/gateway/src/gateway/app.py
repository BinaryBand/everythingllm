"""gateway: the runners' MCP tools over streamable HTTP, for MCP clients other than
AnythingLLM (Claude Code, …), served by uvicorn on the host and reached over HTTPS
through the machine's route (apps.toml). See the README's "MCP gateway".

Its tools are its fronts' (gateway.agents, gateway.research, gateway.sandbox), each front a
group a client may be granted (gateway.grants, grants.toml), its tools named with its
PREFIX. Each call goes to its runner's socket as the host sees it. The sandbox's tools take the client's scope
from its name (grants.client), never from the model.

Every path but /health needs `Authorization: Bearer <token>`, one token per client. A client
sees and calls only the tools it's granted, and each call is logged with the client's name
and the tool's, never its arguments.

Config (environment; the unit reads host.env, then ~/.config/everythingllm/gateway.env,
which holds the tokens, outside the repo and the AnythingLLM container's reach):
  GATEWAY_TOKEN_<NAME>    a client's token; the client is <name>, lowercase, _ as -
                          (at least one; its tools are in grants.toml); <NAME> is at
                          most 63 letters, digits and _ (`uv run hostctl gateway-client`)
  PUBLIC_HOST             the machine's HTTPS name, whose Host header is allowed (from host.env)
  GATEWAY_HOST, GATEWAY_PORT  where to listen (default 127.0.0.1:8452; https is the
                          same port)
  <FRONT>_SOCKET          a runner's socket (default storage/everythingllm/<front>/runner.sock,
                          storage as host.env's ANYTHINGLLM_STORAGE has it)
"""

import hmac
import logging
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

import hostenv
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from gateway import agents, grants, research, sandbox
from gateway.grants import CLIENT_KEY

log = logging.getLogger("gateway")

# Each front's tools are the group named after its runner's folder, named with its PREFIX.
FRONTS = (agents, research, sandbox)

TOKEN_PREFIX = "GATEWAY_TOKEN_"
# A client's name: it names the client's sandbox workspace too (gateway.sandbox), so it
# fits a sandbox key, and it reads the same as a GATEWAY_TOKEN_<NAME> suffix.
CLIENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


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
        bad = sorted(name for name in clients if not CLIENT_RE.fullmatch(name))
        if bad:
            raise SystemExit(
                f"gateway: bad client name(s) {bad} in gateway.env: a {TOKEN_PREFIX}<NAME>'s "
                "name is at most 63 letters, digits and _, not starting or ending with _"
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
        os.environ.setdefault(
            front.ENV, str(hostenv.socket_path(front.FOLDER, front.ENV))
        )


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


def tool_groups() -> dict[str, dict[str, Callable]]:
    """Every group a client may be granted: group -> {tool name -> function}. Two fronts
    with a tool of the same name stop the gateway from starting."""
    groups: dict[str, dict[str, Callable]] = {}
    owner: dict[str, str] = {}
    for front in FRONTS:
        tools = groups.setdefault(front.FOLDER, {})
        for fn in front.tool.registered:
            tool = front.PREFIX + fn.__name__
            if tool in owner:
                raise RuntimeError(
                    f"{front.__name__} and {owner[tool]} both have a tool {tool}"
                )
            owner[tool] = front.__name__
            tools[tool] = fn
    return groups


def build_mcp(
    groups: dict[str, dict[str, Callable]], granted: Mapping[str, Iterable[str]]
) -> MCPServer:
    """The MCP server with every group's tools, each client held to its `granted` groups."""
    unknown = {group for names in granted.values() for group in names} - set(groups)
    if unknown:
        raise ValueError(f"unknown group(s) {sorted(unknown)}")
    allowed = {
        client: {tool for group in names for tool in groups[group]}
        for client, names in granted.items()
    }
    mcp = MCPServer(
        "everythingllm",
        instructions=(
            "EverythingLLM's runners: delegations to AnythingLLM's own agents, deep "
            "research runs and a code sandbox of the client's own. A client has the "
            "tools it was granted."
        ),
        middleware=[grants.Grants(allowed)],
    )
    for tools in groups.values():
        for name, fn in tools.items():
            mcp.add_tool(fn, name=name)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True})

    return mcp


def create_app(
    config: Config, granted: Mapping[str, Iterable[str]] | None = None
) -> ASGIApp:
    """The gateway's ASGI app; `granted` (client -> groups) defaults to grants.toml's."""
    groups = tool_groups()
    if granted is None:
        granted = grants.load(groups)
    for client in sorted(config.clients):
        if client in granted:
            log.info("%s may use %s", client, ", ".join(sorted(granted[client])) or "-")
        else:
            log.warning("%s has a token but no grant in grants.toml: no tools", client)
    hosts = [f"127.0.0.1:{config.port}", f"localhost:{config.port}"]
    if config.public_host:
        hosts += [config.public_host, f"{config.public_host}:{config.port}"]
    app = build_mcp(groups, granted).streamable_http_app(
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
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        log_level="info",
        proxy_headers=True,
        timeout_graceful_shutdown=5,
    )
