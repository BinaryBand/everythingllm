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
  until `uv run hostctl units` renders them (it refuses to run in a worktree). Never edit the installed
  copies; `uv run hostctl diff` shows where they differ.
- After code changes: `uv run hostctl deploy` (syncs skills, jobs, slash commands, system prompt and
  MCP config into storage, runs `mcp-sync`, restarts AnythingLLM, rebuilds sites). It
  restarts AnythingLLM, and so the MCP fronts, but not the runners, host units or service
  containers alike: a runner keeps its old code until `uv run hostctl <app>-setup` or
  `systemctl --user restart <unit>` (a container's unit is `<x>.service` too, and a restart
  is all a code change needs, since it runs the repo mounted read-only). Code shared across
  packages (e.g. `sites.store`, `sites.build`) is loaded by several services (`sites-runner`,
  `audit-runner`, `research-runner`). Don't restart `research-runner` or `agents-runner`
  while a run is going (`hostctl.run_guard` asks; `guard` in the apps registry says which).
  The gateway restarts only by hand.
- Dropped ideas (browser, quiz, whatsapp-mcp) and the history before this repo went public
  are kept in a private archive, not here. Don't recreate them from memory.
- Machine settings come from `host.env` (git-ignored; see `host.env.example`). Unit
  templates use `@KEY@` placeholders, which `hostctl.units` fills in; systemd doesn't
  expand `${VAR}` in `Environment=`.

## Commands

    uv run hostctl                         # list the commands
    uv run hostctl test                    # every Python test, then the skill tests (node, inside the container)
    uv run hostctl test-skills             # just the skill and log filter tests
    uv run hostctl health                  # check every unit, port, runner and MCP server
    uv run hostctl diff / deploy           # what would change live / push it live
    uv run hostctl skills                  # regenerate the forwarding skills from the fronts' `skills`
    uv run hostctl units                   # render and install unit templates, restart what changed
    uv run hostctl <app>-logs / <app>-setup  # follow an app, or (re)start it (`uv run hostctl apps` lists them)

The repo root is one uv workspace (a member per `packages/` subdirectory, one `uv.lock`, the dev
venv in `.venv`). The root `pyproject.toml` holds what every member shares (the `workspace = true`
sources and the dev group); a member's own lists its dependencies, extras and scripts. Run these
from the repo root:

    uv run --all-packages --all-extras pytest -q                       # what hostctl test runs
    uv run --package podcasts --extra host pytest packages/podcasts -q     # one member
    uv run --package podcasts --extra host pytest packages/podcasts/tests/test_scrub.py::test_name -q
    uv lock                                                            # after editing a pyproject.toml

After `uv.lock` changes, run `uv run hostctl mcp-sync` (or `uv run hostctl deploy`) so the container's venv catches
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
- Heavy, long-running or host-dependent work runs in a service outside AnythingLLM:
  `sandbox-runner`, `agents-runner` and `audit-runner` as host units, and `research-runner`,
  `sites-runner` and `podcasts-runner` (with the podcasts' sync and transcription workers)
  in service containers (below). The MCP server or skill in the container is a thin front that forwards each
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
    declared once in `packages/apps/src/apps/apps.toml`, which hostctl and the audit read
    through `packages/apps`; app code never does. Adding one: its code, its unit template
    and an entry there; `packages/apps/tests/test_apps.py` says what's missing (README, "The apps").
- Service containers (README, "Service containers"): a runner that reads the web, feeds
  or audio runs in a Quadlet container (`host/quadlet/<x>.container.in`, the app's
  `container` in `apps.toml`) of one image, `localhost/everythingllm-service`
  (`uv run hostctl service-images`), hardened like the sandbox's: read-only root, every
  capability dropped, `UserNS=keep-id`, memory, CPU and PID limits, and no network but the
  internal `egress-net`. It runs `uv run --frozen --no-dev --project @REPO@ --package <pkg>`
  against the repo mounted read-only, with its venv in `venvs/<x>-ctr/`. Everything it
  mounts is at its host path (`HOME=%h`, so paths mean the same inside and out), and only
  what it uses: never storage, the data dir or `venvs/` whole, never the sandbox's
  `runner.sock` (only `sandbox-build/`, which serves `build_system_site` alone), never
  AnythingLLM's `.env` (its share, written by `hostctl.ctr_env` before each start).
  `packages/egress/tests` hold every template to that. A server in one listens on `0.0.0.0`
  (`LIVE_HOST`, `ARTICLES_HOST`, `RELAY_HOST`), published on the host's `127.0.0.1`, and
  answers only loopback and its own address, where the published port delivers from
  (`hostrpc.local_peer`), never another container on egress-net. It
  reaches AnythingLLM and SearXNG by the tailnet name. Its only way out is the egress proxy
  (`packages/egress`, the `egress` app): it knows a container by its address in
  `egress.toml` and lets it reach public hosts (publicweb's rule) and its profile's
  exceptions on :3128, and public hosts only on :3129, which `publicweb.public_client`
  uses (`EGRESS_PROXY`). A template never sets `ContainerName=`; `hostctl units` retires
  the host unit a container replaces. The relay is in one too. Host processes that write
  where a container can (the sandbox's copy into `pages/public/`, `sites.build`'s marker,
  podcasts-web's reads) open files without following symlinks.
- Podcasts: podcasts-runner only asks; long-running `podcasts-sync-worker` and
  `podcasts-transcribe-worker` take requests from `podcasts/queue/` and keep their own
  schedules (`podcasts.worker`), with no systemd calls and no timers. `podcasts-web`
  (splice) stays a host unit.
- MCP tools only read (or, like `refresh_podcasts`, only start background work). An op that
  writes or acts is a skill (`anythingllm/agent-skills/<op>`), because a skill knows its
  workspace and refuses a delegated task (`_lib/delegated.js`); an MCP call doesn't say where
  it came from. A test holds every skill of ours to that refusal. A skill that only forwards
  an op is declared in its front like a tool, with `@skills.add` (`hostrpc.Skills`), and
  `uv run hostctl skills` generates its `plugin.json` and `handler.js` (`hostrpc.skillgen`); edit the
  declaration, never those files. `uv run hostctl diff` and `uv run hostctl deploy` refuse stale ones.
- Every MCP server is a thin front; nothing it serves runs in the container. Not every
  member is an MCP server: `publicweb`, `llm`, `chatimage` and `hostrpc` are libraries, `hostctl` is the host's command (`uv run hostctl`),
  `splice`, `research` and `sandbox` are services outside AnythingLLM, and `egress` is the
  service containers' proxy. `relay` is an HTTP service
  for the Nilson app, not the agent, in a service container, at `/everythingllm/` on
  AnythingLLM's tailnet :3001; it takes the client's own AnythingLLM key, and its ntfy
  secrets are in `~/.config/everythingllm/relay.env`, never in the repo. `gateway` is the one MCP server on the host: it serves the fronts' own
  tools (each front's `tool.registered`), their skills (wrapped with `hostrpc.forwarder`
  there) and its own fronts' tools (`agents_*`, `research_*`, `sandbox_*`) over HTTP to
  other MCP clients, each with a token in `~/.config/everythingllm/gateway.env`
  (`uv run hostctl gateway-client <name>` adds one) and a grant of tool groups in
  `packages/gateway/src/gateway/grants.toml` (no grant, no tools). Its one MCP middleware
  (`gateway.grants.Grants`) filters and refuses by grant, logs client and tool, and sets
  the ContextVar `gateway.grants.client`, from which the sandbox tools make the scope
  `{workspace: client-<name>, thread: gateway, gateway: true}` (never from the model; the
  sandbox runner keeps `client-` workspaces for such scopes). Research runs from it have no
  workspace and aren't per client. A front's tool or skill reaches it unchanged, so a new
  one needs nothing there; it restarts only by hand.
- The agent does short judgment work through thin tools. For example, the
  `daily-news-page` scheduled job calls `headlines` and then the `write-entry` skill. Code asks a
  model itself (`packages/llm`) only where there's no agent (background syncs, reader clicks,
  long research runs).
- Data only host services use goes in `~/.local/share/everythingllm` (`hostrpc.data_dir()`),
  not in AnythingLLM's storage, laid out by kind: `venvs/<name>` (a container's
  `venvs/<x>-ctr`), `pages/{public,entries}`,
  `sandbox/{workspaces,public}`, `podcasts/` (with `models/`), `research/runs`, `agents/runs`, `relay/`.
  Put new data in the folder of its kind, not at the root. Storage keeps AnythingLLM's own data, the runners' sockets (under `everythingllm/`) and what AnythingLLM
  itself reads (`anythingllm-fs/`, `documents/`).
- Uses `mcp` 2.x: `MCPServer`, not `FastMCP`.
- AnythingLLM's internal API (`/api/...`, not `/api/v1/`) needs its password: call it with
  `hostrpc.anythingllm_headers` (packages) or `units.anythingllm_headers` (tools), never
  without. The developer API (`/api/v1/`) takes the API key instead.

## Sites and pages

- The agent writes entries, not HTML. The `sites` tools save JSON-front-matter Markdown to
  `~/.local/share/everythingllm/pages/entries/<site>/<section>/<slug>.md` (host-only, outside storage), and `sites-runner` (a service container) has the
  sandbox rebuild that site. A write that doesn't build is undone. Other writers use the `sites-write`
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
  site whose `zola.toml` has `[extra.build] theme_from` (every repo site has) is
  built in a sandbox container instead (`build_system_site`), and only there may a theme
  from a workspace's `/shared` be used. Every repo site names one (a test holds them to
  it): `sites-runner`'s container has no zola (`SITES_SANDBOX_ONLY`).
- Templates, stylesheets, `zola.toml` and sections change only in the repo; the agent has
  no tool for them, and `uv run hostctl deploy` rebuilds the sites. The exception is the lab site, an
  experiment the agent owns whole in education's sandbox folder (`/shared/education/sites/lab/`)
  and builds with `build-site`. Agent-written sites and themes are only ever built in a
  sandbox container (`sandbox/sitebuild.py`), never by the host's zola.

## Conventions

- Module docstrings open with what the module is for and list its config under
  `Config (environment):`. Keep them current when you add or change an env var.
- Commit subjects are plain sentences saying what changed and why (e.g. "Decode episodes
  as they're heard, and only hold Whisper's deaths against one"), with no type prefixes.
- `packages/hostctl` (`uv run hostctl <command>`; the commands are in `hostctl.cli`) uses only
  the standard library, so `health.sh` and `before` steps can run it with any `python3`;
  `hostctl.skills` is the one exception.
