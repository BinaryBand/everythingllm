# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This repo is the source of truth for a local AnythingLLM instance and the tools around it: agent skills, the host services behind them, and an MCP gateway that serves the services' tools to other clients. `README.md` is detailed and current; read its section on a subsystem (Deep research, Code sandbox, Browser, Delegation, ...) before changing it.

## This checkout is live

- The AnythingLLM container mounts this directory read-only at `/mcp`, and the host services run its code with uv. Whatever is in the working tree, committed or not, is what runs. Switch branches here for other-branch work; don't use a worktree.
- Units in `host/quadlet/` and `host/systemd/` are templates. Editing them changes nothing until `uv run hostctl units` renders them (it refuses to run in a worktree). Never edit the installed copies; `uv run hostctl diff` shows where they differ.
- After code changes: `uv run hostctl deploy` (syncs the skills and the default prompt and its version variable into storage, restarts AnythingLLM). It restarts AnythingLLM, but not the runners, host units or service containers alike: a runner keeps its old code until `uv run hostctl <app>-setup` or `systemctl --user restart <unit>` (a container's unit is `<x>.service` too, and a restart is all a code change needs, since it runs the repo mounted read-only). Code shared across packages (e.g. `hostrpc`, `runs`, `chatimage`) is loaded by several services. Deploy never removes a skill the repo dropped from storage, nor touches AnythingLLM's own MCP servers or scheduled jobs. Don't restart `research-runner` or `agents-runner` while a run is going (`hostctl.run_guard` asks; `guard` in the apps registry says which). The gateway restarts only by hand.
- Dropped ideas (quiz, whatsapp-mcp, podcasts, the system audit, and the old shared browser that `packages/browser` replaced) and the history before this repo went public are kept in a private archive, not here. Don't recreate them from memory.
- The network is the machine's, not the repo's: it routes each app's `serve` ports and paths (`apps.toml`) from `https://<PUBLIC_HOST>` to `127.0.0.1` (tailscale serve, Caddy, ...), and keeps them to the user's devices. `uv run hostctl routes` lists and checks them; no code here sets them up.
- Machine settings come from `host.env` (git-ignored; see `host.env.example`). Unit templates use `@KEY@` placeholders, which `hostctl.units` fills in; systemd doesn't expand `${VAR}` in `Environment=`.

## Commands

```sh
uv run hostctl                         # list the commands
uv run hostctl test                    # every Python test, then the skill tests (node, inside the container)
uv run hostctl test-skills             # just the skill and log filter tests
uv run hostctl health                  # check every unit, port and runner
uv run hostctl diff / deploy           # what would change live / push it live
uv run hostctl units                   # render and install unit templates, restart what changed
uv run hostctl <app>-logs / <app>-setup  # follow an app, or (re)start it (`uv run hostctl apps` lists them)
```

The repo root is one uv workspace (a member per `packages/` subdirectory, one `uv.lock`, the dev venv in `.venv`). The root `pyproject.toml` holds what every member shares (the `workspace = true` sources and the dev group); a member's own lists its dependencies, extras and scripts. Run these from the repo root:

```sh
uv run --all-packages --all-extras pytest -q                       # what hostctl test runs
uv run --package sandbox pytest packages/sandbox -q                 # one member
uv run --package sandbox pytest packages/sandbox/tests/test_runner.py::test_name -q
uv lock                                                            # after editing a pyproject.toml
```

After `uv.lock` changes, restart the services whose members changed (`uv run hostctl <app>-setup`), which sync their venvs as they start. Don't use `--no-dev` against `.venv`; it uninstalls pytest. `packages/conftest.py` clears `PUBLIC_HOST` and points `ANYTHINGLLM_STORAGE` at a tmp folder and `ANYTHINGLLM_URL`/`ANYTHINGLLM_API` at a port nothing listens on, so tests ignore `host.env` and never reach the live AnythingLLM.

Host services run Python 3.12 (their venvs in `~/.local/share/everythingllm/`); the dev `.venv` is 3.13. Keep code 3.12-compatible, and check with `uv run --python 3.12 --isolated --all-packages --all-extras pytest -q packages/<member>`.

## Architecture: thin skills, host services

- AnythingLLM runs in a privileged rootless-podman container, which can't reach the host's loopback. It runs no MCP server of ours (its MCP servers are its own, set up in its UI); our tools there are skills.
- Heavy, long-running or host-dependent work runs in a service outside AnythingLLM: `sandbox-runner`, `browser-runner` and `agents-runner` as host units, and `research-runner` in a service container (below). The skill in the container is a thin front that forwards each call over `storage/everythingllm/<name>/runner.sock` using `packages/hostrpc`: one request per connection, a line of JSON each way (`{"op","args"}` -> `{"ok","result"|"error"}`).
  - A runner is a `hostrpc.Service` subclass with `op_<name>` methods (or `hostrpc.Service(ops, errors=…)` over plain functions), and its `main()` calls `hostrpc.run(...)`.
  - Skills speak the protocol from node, through `anythingllm/agent-skills/_lib/hostrpc.js` (`_lib/runner.js`'s `forward` for one op): `deep-research`, the sandbox's `run-code`, `write-file`, `publish`, `show-image` and `build-site`, the browser's, and agents-runner's.
  - An MCP client gives up on a tool call after a minute or so (skills have no limit), so an op answers within 45 s. Longer work keeps going in the service (the caller waits on a run id) or in its own systemd unit.
  - Every app (its units, socket, HTTPS routes, guard, health checks, setup steps) is declared once in `packages/hostctl/src/hostctl/apps.toml`, which hostctl reads through `hostctl.apps`; app code never does. Adding one: its code, its unit template and an entry there; `packages/hostctl/tests/test_apps.py` says what's missing (README, "The apps").
- Service containers (README, "Service containers"): a runner that reads the web, feeds or audio runs in a Quadlet container (`host/quadlet/<x>.container.in`, the app's `container` in `apps.toml`) of one image, `localhost/everythingllm-service` (`uv run hostctl service-images`), hardened like the sandbox's: read-only root, every capability dropped, `UserNS=keep-id`, memory, CPU and PID limits, and no network but the internal `egress-net`. It runs `uv run --frozen --no-dev --project @REPO@ --package <pkg>` against the repo mounted read-only, with its venv in `venvs/<x>-ctr/`. Everything it mounts is at its host path (`HOME=%h`, so paths mean the same inside and out), and only what it uses: never storage, the data dir or `venvs/` whole, never the sandbox's `runner.sock`, never AnythingLLM's `.env` (its share, written by `hostctl.ctr_env` before each start). `packages/egress/tests` hold every template to that. A server in one listens on `0.0.0.0` (`LIVE_HOST`, `RELAY_HOST`), published on the host's `127.0.0.1`, and answers only loopback and its own address, where the published port delivers from (`hostrpc.local_peer`), never another container on egress-net. It reaches AnythingLLM and SearXNG by `PUBLIC_HOST`. Its only way out is the egress proxy (`packages/egress`, the `egress` app): it knows a container by its address in `egress.toml` and lets it reach public hosts (publicweb's rule) and its profile's exceptions on :3128, and public hosts only on :3129, which `publicweb.public_client` uses (`EGRESS_PROXY`). A template never sets `ContainerName=`; `hostctl units` retires the host unit a container replaces. The relay is in one too. Host processes that read or write where a container can (agents-runner reading research's report files) open files without following symlinks (`hostrpc.safefs`).
- Our tools in AnythingLLM are skills (`anythingllm/agent-skills/<op>`), not MCP tools, because a skill knows its workspace and refuses a delegated task (`_lib/delegated.js`); an MCP call doesn't say where it came from. A test holds every skill of ours that writes, acts or delegates to that refusal.
- The members: `publicweb`, `llm`, `chatimage`, `hostrpc` and `runs` (run state, slots, run logs and live cards for the runners) are libraries, `hostctl` is the host's command (`uv run hostctl`), `research`, `sandbox`, `agents` and `browser` are services outside AnythingLLM, and `egress` is the service containers' proxy. `relay` is an HTTP service for the Nilson app, not the agent, in a service container, at `/everythingllm/` on AnythingLLM's https :3001; it takes the client's own AnythingLLM key, and its ntfy secrets are in `~/.config/everythingllm/relay.env`, never in the repo. `gateway` is our one MCP server, on the host: it serves its fronts' tools (`agents_*`, `research_*`, `sandbox_*`; signatures with docstrings, sent on by `hostrpc.forwarder(hostrpc.caller(...))`) over HTTP to other MCP clients, each with a token in `~/.config/everythingllm/gateway.env` (`uv run hostctl gateway-client <name>` adds one) and a grant of tool groups in `packages/gateway/src/gateway/grants.toml` (no grant, no tools). Its one MCP middleware (`gateway.grants.Grants`) filters and refuses by grant, logs client and tool, and sets the ContextVar `gateway.grants.client`, from which the sandbox tools make the scope `{workspace: client-<name>, thread: gateway, gateway: true}` (never from the model; the sandbox runner keeps `client-` workspaces for such scopes). Research runs from it have no workspace and are per client (an owner on the run). It restarts only by hand.
- The agent does short judgment work through thin tools. For example, the agent splits a deep research question itself (`sub_questions`) and passes the report on to the user. Code asks a model itself (`packages/llm`) only where there's no agent (long research runs).
- Data only host services use goes in `~/.local/share/everythingllm` (`hostrpc.data_dir()`), not in AnythingLLM's storage, laid out by kind: `venvs/<name>` (a container's `venvs/<x>-ctr`), `pages/public` (the pages site: link cards, shown images), `sandbox/{workspaces,public}` (a workspace's browser profile in `sandbox/workspaces/<ws>/browser/`), `browser/`, `research/runs`, `agents/runs`, `relay/`. Put new data in the folder of its kind, not at the root. Storage keeps AnythingLLM's own data, the runners' sockets (under `everythingllm/`) and what AnythingLLM itself reads (`anythingllm-fs/`, `documents/`). Deep research's reports go in `anythingllm-fs/research/`, and agents-runner puts each in its workspace's documents through the developer API.
- Uses `mcp` 2.x: `MCPServer`, not `FastMCP`.
- AnythingLLM's internal API (`/api/...`, not `/api/v1/`) needs its password: call it with `hostrpc.anythingllm_headers` (packages) or `hostctl.units.anythingllm_headers` (hostctl), never without. The developer API (`/api/v1/`) takes the API key instead.

## Pages

- A sandbox workspace's `/public` is its pages, kept in `~/.local/share/everythingllm/sandbox/public/<workspace>/` (a tree holding nothing else) and served as it is by Caddy on :8447 under `/<workspace>/`, with no copy or sync. That port is an origin of its own, and its CSP runs every page's scripts in a sandbox (`allow-scripts allow-downloads`, an opaque origin per page); never add `allow-same-origin`, `allow-forms` or `allow-popups`.
- The pages site on :8445 (`host/caddy/pages.Caddyfile`, a strict CSP: no scripts, nothing from other hosts, no forms) serves what the host draws or keeps for the chat: link cards (`_cards/`), the images `show-image` puts in the chat (`_images/`), and the old research site's reports.
- zola is only in the sandbox image: a site the agent builds (`build-site`, `sandbox/sitebuild.py`) is built in a sandbox container with no network and a time limit, with the repo's theme (`packages/sandbox/zola/themes/agent-site/`, Tera 2 `{% component %}`s, not macros) or one from a workspace's `/shared`. The lab site, in education's `/shared/education/sites/lab/`, is the agent's own. Tests that build a real site run zola in that image through podman (the `sandbox_zola` fixture in `packages/conftest.py`).

## Conventions

- Module docstrings open with what the module is for and list its config under `Config (environment):`. Keep them current when you add or change an env var.
- Commit subjects are plain sentences saying what changed and why (e.g. "Decode episodes as they're heard, and only hold Whisper's deaths against one"), with no type prefixes.
- `packages/hostctl` (`uv run hostctl <command>`; the commands are in `hostctl.cli`) uses only the standard library, so `health.sh` and `before` steps can run it with any `python3`.
