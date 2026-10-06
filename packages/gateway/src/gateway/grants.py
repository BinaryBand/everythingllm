"""Which of the gateway's tools each client may see and call, and who is calling.

grants.toml, next to this file, gives each client the groups of tools it gets, read with
`load` like packages/apps reads apps.toml. A client is named as its token is in gateway.env;
the tokens stay there, never in the repo. A group is a front's read tools (`sites`), its
skills (`sites:write`), or a front declared in the gateway (`agents`, `research`,
`sandbox`); gateway.app says which groups there are. A client with a token but no grant gets no tools, and a key or group the
file doesn't know is an error, so a typo stops the gateway rather than widening a grant.

`Grants` is the gateway's one MCP middleware. It drops from tools/list what the client isn't
granted, refuses a tools/call outside its grant, logs each call by client and tool (never
its arguments or the token), and sets `client` around the rest of the chain, which is how a
tool learns who is calling it.
"""

import logging
from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import tomllib
from mcp.shared.exceptions import MCPError
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS

log = logging.getLogger("gateway")

GRANTS = Path(__file__).with_name("grants.toml")
CLIENT_FIELDS = {"tools"}

# Where gateway.app's RequireToken leaves the client's name in the HTTP request's scope.
CLIENT_KEY = "gateway.client"

# The calling client's name while its request runs (None outside one).
client: ContextVar[str | None] = ContextVar("gateway_client", default=None)


def load(groups: Iterable[str], path: Path = GRANTS) -> dict[str, frozenset[str]]:
    """Each listed client's groups, by client; ValueError for a key or a group (not one of
    `groups`) it doesn't know."""
    with path.open("rb") as f:
        raw = tomllib.load(f)
    unknown = set(raw) - {"clients"}
    if unknown:
        raise ValueError(f"{path.name}: unknown key(s) {sorted(unknown)}")
    known = set(groups)
    grants = {}
    for name, grant in raw.get("clients", {}).items():
        where = f"{path.name} [clients.{name}]"
        if not isinstance(grant, dict):
            raise ValueError(f"{where}: not a table")  # noqa: TRY004 - a bad file
        unknown = set(grant) - CLIENT_FIELDS
        if unknown:
            raise ValueError(f"{where}: unknown field(s) {sorted(unknown)}")
        tools = grant.get("tools", [])
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise ValueError(f"{where}: tools must be a list of group names")
        unknown = set(tools) - known
        if unknown:
            raise ValueError(
                f"{where}: unknown group(s) {sorted(unknown)}; the groups are "
                f"{', '.join(sorted(known))}"
            )
        grants[name] = frozenset(tools)
    return grants


class Grants:
    """MCP middleware (a ServerMiddleware): holds each client to its tools, given as
    client -> the names of the tools it may use. A client it doesn't list, or a request
    without one, gets none."""

    def __init__(self, allowed: Mapping[str, Iterable[str]]) -> None:
        self.allowed = {name: frozenset(tools) for name, tools in allowed.items()}

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        scope = getattr(ctx.request, "scope", None) or {}
        name = scope.get(CLIENT_KEY)
        allowed = self.allowed.get(name, frozenset()) if name else frozenset()
        if ctx.method == "tools/call":
            tool = (ctx.params or {}).get("name")
            if not isinstance(tool, str) or tool not in allowed:
                # %r: the name is the client's text, so it stays on one line.
                log.warning("%s was refused %r", name or "?", tool)
                raise MCPError(
                    code=INVALID_PARAMS,
                    message=f"Tool {tool!r} isn't one this gateway client ({name or '?'}) "
                    "may call: it's not among the tools it was granted.",
                )
            log.info("%s called %s", name, tool)
        token = client.set(name)
        try:
            result = await call_next(ctx)
        finally:
            client.reset(token)
        if ctx.method == "tools/list":
            # The handler's result is the wire's dict by now; anything else isn't
            # passed on unfiltered.
            if not isinstance(result, dict):
                raise MCPError(code=INTERNAL_ERROR, message="tools/list gave no tools")
            result = {
                **result,
                "tools": [
                    t for t in result.get("tools", ()) if t.get("name") in allowed
                ],
            }
        return result
