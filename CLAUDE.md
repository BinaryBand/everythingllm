# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This repo is the source of truth for a local AnythingLLM instance and the tools around it:
MCP servers, agent skills, scheduled jobs, Zola sites and the host services behind them.
`README.md` is detailed and current; read its section on a subsystem (Podcasts, Deep
research, Code sandbox, System audit, …) before changing it.

## This checkout is live

- The AnythingLLM container mounts this directory read-only at `/mcp`, and the host
  services run its code with uv. Whatever is in the working tree, committed or not, is what
  runs. Don't switch branches here; use a worktree for other-branch work.
- Units in `host/quadlet/` and `host/systemd/` are templates. Editing them changes nothing
  until `make units` renders them (it refuses to run in a worktree). Never edit the installed
  copies; `make diff` shows where they differ.
- After code changes: `make deploy` (syncs skills, jobs, slash commands, system prompt and
  MCP config into storage, runs `mcp-sync`, restarts AnythingLLM, rebuilds sites). It
  restarts AnythingLLM, and so the MCP fronts, but not the host services: a runner keeps its
  old code until `make <name>-setup` or `systemctl --user restart <unit>`. Code shared
  across packages (e.g. `sites.store`) is loaded by several services (`sites-runner`,
  `audit-runner`, `research-runner`). Don't restart `research-runner`
  while a run is going.
- Dropped ideas (browser, quiz, whatsapp-mcp) and the history before this repo went public
  are kept in a private archive, not here. Don't recreate them from memory.
- Machine settings come from `host.env` (git-ignored; see `host.env.example`). Unit
  templates use `@KEY@` placeholders, which `scripts/units.py` fills in; systemd doesn't
  expand `${VAR}` in `Environment=`.

## Commands

    make test                      # every Python test, then the skill tests (node, inside the container)
    make test-skills               # just the skill and log filter tests
    make health                    # check every unit, port, runner and MCP server
    make diff / make deploy        # what would change live / push it live
    make units                     # render and install unit templates, restart what changed
    make <name>-logs               # follow a service (research, podcasts, sites, audit, sandbox, …)

The repo root is one uv workspace (a member per `src/mcps/` subdirectory, one `uv.lock`, the dev
venv in `.venv`). The root `pyproject.toml` holds what every member shares (the `workspace = true`
sources and the dev group); a member's own lists its dependencies, extras and scripts. Run these
from the repo root:

    uv run --all-packages --all-extras pytest -q                       # what make test runs
    uv run --package podcasts --extra host pytest src/mcps/podcasts -q     # one member
    uv run --package podcasts --extra host pytest src/mcps/podcasts/tests/test_scrub.py::test_name -q
    uv lock                                                            # after editing a pyproject.toml

After `uv.lock` changes, run `make mcp-sync` (or `make deploy`) so the container's venv catches
up. Don't use `--no-dev` against `.venv`; it uninstalls pytest. `src/mcps/conftest.py`
clears `PUBLIC_HOST` and `ANYTHINGLLM_STORAGE`, so tests ignore `host.env`.

Host services run Python 3.12 (their venvs in `~/.local/share/everythingllm/`); the dev `.venv`
is 3.13. Keep code 3.12-compatible, and check with
`uv run --python 3.12 --isolated --all-packages --all-extras pytest -q src/mcps/<member>`.

## Architecture: thin fronts, host services

- AnythingLLM runs in a privileged rootless-podman container. Its MCP servers
  (`anythingllm/mcp_servers.json`) run inside it over stdio, started with
  `uv run --frozen --project /mcp --package <name>`. The container can't reach the
  host's loopback.
- Heavy, long-running or host-dependent work runs in a host service:
  `sandbox-runner`, `research-runner`, `podcasts-runner`, `sites-runner` and
  `audit-runner`. The MCP server or skill in the container is a thin front that forwards each
  call over `storage/<name>/runner.sock` using `src/mcps/hostrpc`: one request per connection,
  a line of JSON each way (`{"op","args"}` → `{"ok","result"|"error"}`).
  - A runner is `hostrpc.Service(tools.OPS, errors=…)`: its ops are the functions in the
    package's `tools.py`, which also holds `main()` (`hostrpc.run(...)`). A front's tools are
    signatures with docstrings and no body, registered by
    `hostrpc.forwarder(hostrpc.caller(folder, ENV, name, error=ToolError), mcp.add_tool)`.
    `src/mcps/podcasts` (`server.py`, `tools.py`) is the reference example; research and the
    sandbox keep state, so theirs are `Service` subclasses with `op_<name>` methods.
  - Skills speak the same protocol from node, through `anythingllm/agent-skills/_lib/hostrpc.js`:
    `deep-research`, and the sandbox's `run-code`, `write-file` and `publish`.
  - AnythingLLM drops an MCP tool call after 60 s (skills have no limit), so an op answers within 45 s. Longer work keeps
    going in the service (the caller waits on a run id) or in its own systemd unit.
  - A front's package keeps its base dependencies to what the front imports, and puts the
    rest (Whisper, Kokoro, PyAV, …) in a `host` extra that the units run with.
  - Adding a service: the README's "Services on the host" lists every piece (console
    script, unit, the audit's `WATCHED` in `audit/services.py`).
- Every MCP server is a thin front; nothing it serves runs in the container. Not every
  member is an MCP server: `publicweb`, `llm` and `hostrpc` are libraries, and `splice`,
  `research` and `sandbox` are host-only services. `src/relay` (outside `src/mcps/`) is a
  host HTTP service for the Nilson app, not the agent; its secrets are in
  `~/.config/everythingllm/relay.env`, never in the repo.
- The agent does short judgment work through thin tools. For example, the
  `daily-news-page` scheduled job calls `headlines` and then `write_entry`. Code asks a
  model itself (`src/mcps/llm`) only where there's no agent (background syncs, reader clicks,
  long research runs).
- Uses `mcp` 2.x: `MCPServer`, not `FastMCP`.

## Sites and pages

- The agent writes entries, not HTML. The `sites` tools save JSON-front-matter Markdown to
  `storage/zola/<site>/<section>/<slug>.md`, and `sites-runner` rebuilds that site with the
  host's zola. A write that doesn't build is undone. Other writers use the `sites-write`
  command or `SiteStore`, so the entry format has one implementation.
- The exception is free-form pages: the `publish` skill has `sandbox-runner` copy a file or
  folder from the sandbox to `storage/site/<slug>/`, with a `.page` marker naming the
  workspace that owns it; Caddy allows inline CSS in marked folders.
- Sites live in `zola/sites/<name>/` and share the `zola/themes/agent-site/` theme
  (Tera 2 `{% component %}`s, not macros). Each site documents its fields for the agent in
  `agent_help` in its `zola.toml`.
- The pages site is served by Caddy (`host/caddy/pages.Caddyfile`) under a strict CSP: no
  scripts, nothing from other hosts, no forms. Templates must work without scripts or
  inline styles, and pass `sites.lint`; a test holds every repo template to it.
- zola always builds without a network (`unshare --net`), with a 40 s limit (`sites.build`).
- Templates, stylesheets, `zola.toml` and sections change only in the repo; the agent has
  no tool for them, and `make deploy` rebuilds the sites.

## Conventions

- Module docstrings open with what the module is for and list its config under
  `Config (environment):`. Keep them current when you add or change an env var.
- Commit subjects are plain sentences saying what changed and why (e.g. "Decode episodes
  as they're heard, and only hold Whisper's deaths against one"), with no type prefixes.
- `scripts/sync.py` uses only the standard library and runs with the system `python3`.
