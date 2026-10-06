"""The services the audit and `uv run hostctl health` look at, from the apps registry
(packages/apps). Nothing else is imported here, so the MCP server in the container can
name them without loading the checks."""

import apps

# Services whose logs we read, by the journal field that names them: our containers
# and our host user units.
WATCHED = apps.watched()

# Host services the MCP servers and skills hand work to, by the folder their socket is in
# under storage/everythingllm/.
RUNNERS = apps.runners()
