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
  `audit-runner`, `research-runner`). Don't restart `research-runner` or `agents-runner`
  while a run is going (`tools/run_guard.py` asks; `guard` in the apps registry says which).
- Dropped ideas (browser, quiz, whatsapp-mcp) and the history before this repo went public
  are kept in a private archive, not here. Don't recreate them from memory.
- Machine settings come from `host.env` (git-ignored; see `host.env.example`). Unit
  templates use `@KEY@` placeholders, which `tools/units.py` fills in; systemd doesn't
  expand `${VAR}` in `Environment=`.

## Commands

    make test                      # every Python test, then the skill tests (node, inside the container)
    make test-skills               # just the skill and log filter tests
    make health                    # check every unit, port, runner and MCP server
    make diff / make deploy        # what would change live / push it live
    make skills                    # regenerate the forwarding skills from the fronts' `skills`
    make units                     # render and install unit templates, restart what changed
    make <app>-logs / <app>-setup  # follow an app, or (re)start it (`make apps` lists them)

The repo root is one uv workspace (a member per `packages/` subdirectory, one `uv.lock`, the dev
venv in `.venv`). The root `pyproject.toml` holds what every member shares (the `workspace = true`
sources and the dev group); a member's own lists its dependencies, extras and scripts. Run these
from the repo root:

    uv run --all-packages --all-extras pytest -q                       # what make test runs
    uv run --package podcasts --extra host pytest packages/podcasts -q     # one member
    uv run --package podcasts --extra host pytest packages/podcasts/tests/test_scrub.py::test_name -q
    uv lock                                                            # after editing a pyproject.toml

After `uv.lock` changes, run `make mcp-sync` (or `make deploy`) so the container's venv catches
up. Don't use `--no-dev` against `.venv`; it uninstalls pytest. `packages/conftest.py`
clears `PUBLIC_HOST` and `ANYTHINGLLM_STORAGE`, so tests ignore `host.env`.

Host services run Python 3.12 (their venvs in `~/.local/share/everythingllm/`); the dev `.venv`
is 3.13. Keep code 3.12-compatible, and check with
`uv run --python 3.12 --isolated --all-packages --all-extras pytest -q packages/<member>`.

## Architecture: thin fronts, host services

- AnythingLLM runs in a privileged rootless-podman container. Its MCP servers
  (`anythingllm/mcp_servers.json`) run inside it over stdio, started with
  `uv run --frozen --project /mcp --package <name>`. The container can't reach the
  host's loopback.
- Heavy, long-running or host-dependent work runs in a host service:
  `sandbox-runner`, `research-runner`, `agents-runner`, `podcasts-runner`, `sites-runner` and
  `audit-runner`. The MCP server or skill in the container is a thin front that forwards each
  call over `storage/everythingllm/<name>/runner.sock` using `packages/hostrpc`: one request per connection,
  a line of JSON each way (`{"op","args"}` → `{"ok","result"|"error"}`).
  - A runner is `hostrpc.Service(tools.OPS, errors=…)`: its ops are the functions in the
    package's `tools.py`, which also holds `main()` (`hostrpc.run(...)`). A front's tools are
    signatures with docstrings and no body, registered by
    `hostrpc.forwarder(hostrpc.caller(folder, ENV, name, error=ToolError), mcp.add_tool)`.
    `packages/podcasts` (`server.py`, `tools.py`) is the reference example; research and the
    sandbox keep state, so theirs are `Service` subclasses with `op_<name>` methods.
  - Skills speak the same protocol from node, through `anythingllm/agent-skills/_lib/hostrpc.js`:
    `deep-research`, the sandbox's `run-code`, `write-file`, `publish` and `build-site`, and
    the runners' ops that write or act (`write-entry`, `add-podcast`, `publish-report`, …).
  - AnythingLLM drops an MCP tool call after 60 s (skills have no limit), so an op answers within 45 s. Longer work keeps
    going in the service (the caller waits on a run id) or in its own systemd unit.
  - A front's package keeps its base dependencies to what the front imports, and puts the
    rest (Whisper, PyAV, …) in a `host` extra that the units run with.
  - Every app (its units, socket, tailnet mappings, guard, health checks, setup steps) is
    declared once in `packages/apps/src/apps/apps.toml`, which the tools and the audit read
    through `packages/apps`; app code never does. Adding one: its code, its unit template
    and an entry there; `packages/apps/tests/test_apps.py` says what's missing (README, "The apps").
- MCP tools only read (or, like `refresh_podcasts`, only start background work). An op that
  writes or acts is a skill (`anythingllm/agent-skills/<op>`), because a skill knows its
  workspace and refuses a delegated task (`_lib/delegated.js`); an MCP call doesn't say where
  it came from. A test holds every skill of ours to that refusal. A skill that only forwards
  an op is declared in its front like a tool, with `@skills.add` (`hostrpc.Skills`), and
  `make skills` generates its `plugin.json` and `handler.js` (`hostrpc.skillgen`); edit the
  declaration, never those files. `make diff` and `make deploy` refuse stale ones.
- Every MCP server is a thin front; nothing it serves runs in the container. Not every
  member is an MCP server: `publicweb`, `llm`, `chatimage` and `hostrpc` are libraries, and
  `splice`, `research` and `sandbox` are host-only services. `relay` is a host HTTP service
  for the Nilson app, not the agent; its secrets are in `~/.config/everythingllm/relay.env`,
  never in the repo.
- The agent does short judgment work through thin tools. For example, the
  `daily-news-page` scheduled job calls `headlines` and then the `write-entry` skill. Code asks a
  model itself (`packages/llm`) only where there's no agent (background syncs, reader clicks,
  long research runs).
- Data only host services use goes in `~/.local/share/everythingllm` (`hostrpc.data_dir()`),
  not in AnythingLLM's storage, laid out by kind: `venvs/<name>`, `pages/{public,entries}`,
  `sandbox/{workspaces,public}`, `podcasts/` (with `models/`), `research/runs`, `agents/runs`, `relay/`.
  Put new data in the folder of its kind, not at the root. Storage keeps AnythingLLM's own data, the runners' sockets (under `everythingllm/`) and what AnythingLLM
  itself reads (`anythingllm-fs/`, `documents/`).
- Uses `mcp` 2.x: `MCPServer`, not `FastMCP`.
- AnythingLLM's internal API (`/api/...`, not `/api/v1/`) needs its password: call it with
  `hostrpc.anythingllm_headers` (packages) or `units.anythingllm_headers` (tools), never
  without. The developer API (`/api/v1/`) takes the API key instead.

## Sites and pages

- The agent writes entries, not HTML. The `sites` tools save JSON-front-matter Markdown to
  `~/.local/share/everythingllm/pages/entries/<site>/<section>/<slug>.md` (host-only, outside storage), and `sites-runner` rebuilds that site with the
  host's zola. A write that doesn't build is undone. Other writers use the `sites-write`
  command or `SiteStore`, so the entry format has one implementation.
- The exception is free-form pages: a sandbox workspace's `/public` is its pages, kept in
  `~/.local/share/everythingllm/sandbox/public/<workspace>/` (a tree holding nothing else)
  and served as it is by Caddy on :8447 under `/<workspace>/`, with no copy or sync.
  That port is an origin of its own; its scripts are off, and the Caddyfile's `@scripts`
  is the switch for one workspace.
- Sites live in `packages/sites/zola/sites/<name>/` and share the `packages/sites/zola/themes/agent-site/` theme
  (Tera 2 `{% component %}`s, not macros). Each site documents its fields for the agent in
  `agent_help` in its `zola.toml`.
- The pages site is served by Caddy (`host/caddy/pages.Caddyfile`) under a strict CSP: no
  scripts, nothing from other hosts, no forms. Templates must work without scripts or
  inline styles, and pass `sites.lint`; a test holds every repo template to it.
- zola always builds without a network (`unshare --net`), with a 40 s limit (`sites.build`). A
  site whose `zola.toml` has `[extra.build] theme_from` (news, research and status do) is
  built in a sandbox container instead (`build_system_site`), and only there may a theme
  from a workspace's `/shared` be used.
- Templates, stylesheets, `zola.toml` and sections change only in the repo; the agent has
  no tool for them, and `make deploy` rebuilds the sites. The exception is the lab site, an
  experiment the agent owns whole in education's sandbox folder (`/shared/education/sites/lab/`)
  and builds with `build-site`. Agent-written sites and themes are only ever built in a
  sandbox container (`sandbox/sitebuild.py`), never by the host's zola.

## Conventions

- Module docstrings open with what the module is for and list its config under
  `Config (environment):`. Keep them current when you add or change an env var.
- Commit subjects are plain sentences saying what changed and why (e.g. "Decode episodes
  as they're heard, and only hold Whisper's deaths against one"), with no type prefixes.
- `tools/sync.py` uses only the standard library and runs with the system `python3`.
