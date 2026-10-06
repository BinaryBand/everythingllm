import asyncio
import json
import sys

from audit import health
from audit.services import WATCHED

SERVER = """
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("fake")

@mcp.tool()
def one() -> str:
    return "1"

@mcp.tool()
def two() -> str:
    return "2"

mcp.run()
"""


def test_units_come_from_the_audits_watch_list():
    units = health.units()
    assert len(units) == len(WATCHED)
    assert "anythingllm.service" in units and "sandbox-runner.service" in units
    assert "egress-proxy.service" in units  # a container of ours: Quadlet's unit
    assert WATCHED["systemd-egress-proxy"] == ("CONTAINER_NAME", "egress proxy")
    assert all(u.endswith(".service") and not u.startswith("systemd-") for u in units)


def test_mcp_servers_start_and_list_tools_or_fail(tmp_path):
    (tmp_path / "server.py").write_text(SERVER)
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fake": {
                        "command": sys.executable,
                        "args": [str(tmp_path / "server.py")],
                    },
                    "broken": {
                        "command": sys.executable,
                        "args": ["-c", "raise SystemExit(1)"],
                    },
                }
            }
        )
    )
    results = {
        text.split()[1].rstrip(":"): (good, text)
        for good, text in asyncio.run(health.mcp_servers(str(config)))
    }
    assert results["fake"] == (True, "MCP fake (2 tools)")
    assert results["broken"][0] is False
