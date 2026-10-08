# EverythingLLM

Source of truth for the local AnythingLLM instance (`anythingllm.service`, a Quadlet unit rendered from `host/quadlet/` into `~/.config/containers/systemd/`, storage in `ANYTHINGLLM_STORAGE`, on this machine `/srv/anythingllm/storage`).

## Setting up a machine

On a new machine, or to bring this one up to date:

```sh
git clone <repo> && cd everythingllm         # any folder; the units are rendered with its path
cp host.env.example host.env && $EDITOR host.env
uv run hostctl install
```

`uv run hostctl install` first checks the machine and stops with a list of what's missing. It checks `host.env`, the tools the units run (podman and uv at fixed paths), lingering and the storage folder, creating the folders the containers mount inside it. Then it does the following:

1. renders and starts the units (`uv run hostctl units`)
1. waits for AnythingLLM
1. points web search at SearXNG
1. runs every setup target: the sandbox, the egress proxy, the browser and research
1. deploys (`uv run hostctl deploy`), after the setups, since it deploys only the skills of the apps set up here
1. runs `uv run hostctl health`, which checks the machine's routes (see "The machine's routes")
1. ends with a checklist of what only AnythingLLM's UI can do. Each item is ticked when it's already done: the chat model and embedder, a DeepSeek key, the agent limits in the `.env`, create-scheduled-job off and no job or file tool running without asking, the machine's routes answering, SearXNG answering, a workspace, the built-in skills to turn off, Gmail.

Every step only changes what's out of date, so running it again is safe.

## Host config

This machine's settings live in `host.env` at the repo root, which git ignores. To set it up, copy `host.env.example` and fill it in:

- `PUBLIC_HOST`: the name this machine is reached by over HTTPS. The machine routes the apps' ports on it (see "The machine's routes"), the service containers reach AnythingLLM and SearXNG by it, and every link the setup hands out uses it.
- `ANYTHINGLLM_STORAGE`: AnythingLLM's storage directory on the host.

These read it:

- hostctl, which passes both on to what it runs
- `hostctl.sync`, for `ANYTHINGLLM_STORAGE`
- the host's systemd units, through `EnvironmentFile=@REPO@/host.env` (filled in by `uv run hostctl units`)

Code running on the host derives its storage paths from `ANYTHINGLLM_STORAGE`. Inside the container that variable isn't set, and storage is `/app/server/storage`. Tests ignore `host.env`, so they run the same on any machine.

### The machine's routes

The network is the machine's, not the repo's. Nothing here runs `tailscale serve` or configures a proxy; the machine provides, by whatever it likes (tailscale serve, Caddy, nginx, ...), an HTTPS route for each app's `serve` entry in `apps.toml`: `https://<PUBLIC_HOST>:<https><path>` to `http://127.0.0.1:<port>`. `uv run hostctl routes` lists them and checks each answers, and `uv run hostctl health` and the install checklist do too; an app `install` leaves out (agents, the relay, the gateway) counts once one of its units runs. Today they are:

| Port | Path | To | App |
| --- | --- | --- | --- |
| 3001 | `/` | :3001 | AnythingLLM |
| 3001 | `/everythingllm` | :8446 | the Nilson relay (when set up) |
| 8445 | `/` | :8445 | the pages site (Caddy) |
| 8445 | `/_live/browser` | :8453 | browser live cards |
| 8445 | `/_live/research` | :8450 | research live cards |
| 8445 | `/_live/agents` | :8451 | delegation live cards (when set up) |
| 8445 | `/_live/apps` | :8455 | app cards (the sandbox runner's apps server) |
| 8447 | `/` | :8447 | the workspace pages (Caddy) |
| 8447 | `/_apps` | :8455 | apps' write-back (the sandbox runner's apps server) |
| 8452 | `/` | :8452 | the MCP gateway (when set up) |
| 8454 | `/` | :8454 | the browser take-over view |
| 8888 | `/` | :8888 | SearXNG |

What the code counts on from them:

- **They connect from the host's 127.0.0.1.** The relay, the live cards and the take-over view answer only loopback and their container's own address (`hostrpc.local_peer`), and believe `X-Forwarded-For` and `X-Forwarded-Proto` only from there. A proxy on the host works; one in a container on a bridge network doesn't.
- **A path's prefix may be stripped or not.** Every server under a path takes its routes with or without it.
- **Responses aren't buffered.** The live cards are `multipart/x-mixed-replace` streams, and the relay streams chat answers; each part has to go on as it comes (nginx: `proxy_buffering off`).
- **A valid certificate for `PUBLIC_HOST`.** Every link is https, and the service containers check it when they reach AnythingLLM and SearXNG through the egress proxy.
- **`PUBLIC_HOST` resolves to an address of this machine where the routes listen, not loopback.** The egress proxy resolves it through podman's network, as the host does, and connects there; on 127.0.0.1 it would reach its own container. `routes` fails on that.
- **Only your own devices reach them.** This is the repo's one assumption about who's calling: the pages sites and the live cards ask no one, AnythingLLM asks for its password, the take-over view for its token and the gateway for its clients'. `routes` warns when `PUBLIC_HOST` resolves to a public address. A tailnet, a LAN or a VPN all do.

### Containers

This repo owns the containers the setup runs, as templates in `host/quadlet/`; besides the service containers (see "Service containers"), these two:

- `anythingllm.container`: AnythingLLM, pinned by digest, because what it preloads from `anythingllm/` depends on its internals: the log filter, `thread-scope.js`, which gives skills an API chat's thread, and `agent-stop.js`, which stops an API chat's agent when its client goes

- `static_agent.container`: a Caddy container that mounts `host/caddy/pages.Caddyfile` from the repo, so its CSPs are versioned, and serves two sites:

  - **the pages site** (:8445): the link cards, the images `show-image` puts in the chat, and the research site's old reports, from `pages/public/`. `default-src 'self'; script-src 'none'`: no scripts, no inline styles, and nothing fetched from another host, so CSS can't send anything out either. `form-action 'none'; base-uri 'none'` cover what `default-src` doesn't: no form posts anywhere, and no `<base>` repoints a page's links. Its front page and the workspace pages' old addresses redirect to :8447.
  - **the workspace pages site** (:8447): every sandbox workspace's `/public`, mounted read-only from `sandbox/public/` and served as it is (see "Code sandbox"). It's a port, and so a browser origin, of its own, so that whatever its pages run can't act as the pages site. The same policy, but inline CSS is allowed, and so are inline scripts and scripts from the site itself (`script-src 'self' 'unsafe-inline'`), in every workspace, only ever in a CSP sandbox: `sandbox allow-scripts allow-downloads`. Each page gets an opaque origin of its own, so its scripts can't use storage or cookies, read the site's other pages and files (not even its own folder's, with `fetch`), load module scripts, submit forms, open windows or new tabs, or show `alert()`s; downloads and Caddy's directory listing still work. `allow-same-origin` must never be added: it would let one workspace's scripts read and rewrite every other's pages. `packages/sandbox/tests/test_pages_browser.py` checks all of this in a real Chromium.

  `publish` and the sandbox's replies warn the agent when a page uses something the CSP blocks (scripts, stylesheets, fonts or images from other hosts), since the page would otherwise just render without it. They also pass on the page's notices: that it has scripts, so the agent tells the user what they do and asks before publishing it, and which of the sandbox's limits (storage, `fetch`, module scripts, alerts, `target=_blank` links, forms) it runs into.

The host's own units in `host/systemd/` are templates too. `uv run hostctl units` renders all of them:

- `host/quadlet/*.container.in` goes to `~/.config/containers/systemd/`
- `host/systemd/*.service` and `*.timer` go to `~/.config/systemd/user/`

It fills in `@REPO@` (the checkout's path) and the `host.env` settings, overwrites what's installed (git has the templates' history), then reloads systemd and restarts what changed: a container whose unit changed, or a host unit that's running. A change to comments alone restarts nothing. A guarded runner with a run going is left running, and a container whose image of ours or network isn't there yet isn't started: its app's setup makes them (see "Service containers"). Nor is one whose egress proxy (its `Wants=`) isn't installed yet: `uv run hostctl units egress` comes first. Enabling a host unit is up to its app's `uv run hostctl <app>-setup` (see "The apps" below). A host unit it rendered whose template is gone is retired: stopped, disabled and deleted. That is how a dropped app's units go, and how a host runner gives way to its container, whose Quadlet unit of the same name the old copy would hide (the container is started then, unless it's a guarded runner with a run going). While one of an app's containers can't start yet, every old host unit of that app stays as it is, so the app is never left with neither. A container it rendered whose template is gone is stopped and deleted, before anything is written, so a host unit of its name can't take over while it runs; never AnythingLLM's or the egress proxy's, which it leaves with a word on why. `uv run hostctl diff` lists what it would retire. Units it didn't render are left alone. Given app names, `uv run hostctl units relay` installs and retires only those apps' units (a unit the registry no longer has counts as an app's by its name), so services move into their containers one at a time; the rest wait for a later run.

Run it from the main checkout. It refuses to run in a worktree, since the units run the repo they were rendered from. Edit the templates, never the installed copies; `uv run hostctl diff` shows where the two differ.

An Ansible playbook used to install the two containers' units and `/srv/static-agent-config/`. It must leave them alone now, or its next run undoes `uv run hostctl units`.

### The apps

Every app this repo runs is declared once, in `packages/hostctl/src/hostctl/apps.toml`: its units and a label for each, its socket, the HTTPS routes it needs from the machine, whether its restarts wait for a run (the guard), its health checks, the steps its setup runs first, whether `uv run hostctl install` sets it up (and if not, why), and its skills. hostctl reads it through `hostctl.apps` (standard library only, like the rest of hostctl); app code never does. `uv run hostctl apps` lists the apps; for each:

- `uv run hostctl <app>-setup` installs its own units (`uv run hostctl units <app>`, so another app's runner never moves into its container on the side), runs its `before` steps (the sandbox's and the service containers' image builds, the agents and gateway key files, the relay's settings file), enables and (re)starts its units and (re)starts its containers, asking first while a guarded one has a run going (`FORCE=1` doesn't ask), starts its timers, and prints the routes it needs from the machine (`hostctl.appctl`).
- `uv run hostctl <app>-logs` follows its units and the ones it watches.
- `uv run hostctl deploy` copies an app's skills into AnythingLLM only while the app is set up here, which is while its runner is enabled (its setup enables a host unit; a container's unit counts once `uv run hostctl units` installed it), and takes them out of storage otherwise, so the agent isn't offered a tool whose runner isn't there. So a machine can run some apps and not others: the browser without the sandbox, say, or no delegations until agents-runner has its key. `<app>-setup` says when the app's skills are still waiting for a deploy.
- `uv run hostctl routes` lists every app's routes on `PUBLIC_HOST` and checks each answers (see "The machine's routes"); it sets nothing up.
- `uv run hostctl health` checks every app's units, health URLs, routes and sockets (`health.sh`, which gets them from `python3 -m hostctl.appctl units`, `health`, `routes` and `sockets`, pinging each runner).

Adding an app: its code, its unit template in `host/`, and one entry in `apps.toml`, listing its skills. `packages/hostctl/tests/test_apps.py` says what's missing: a template no app owns, a unit without a template, two mappings on one port, a port that isn't the one the code or the unit uses, or a skill no app lists (or one that never calls its app's runner).

### AnythingLLM's password

AnythingLLM's own API (`/api/...`, which its UI uses; not the developer API's `/api/v1/`) answers anyone who reaches it until it has a password, and the machine's :3001 route reaches it. That includes scheduled jobs, which run the agent with every tool approved, `.env` changes and new API keys. So it gets a password (Settings > Security > Password protection; long and random, from `[a-zA-Z0-9_-!@$%^&*();]`), which AnythingLLM keeps in plain text as `AUTH_TOKEN` in storage's `.env`, beside a `JWT_SECRET` it makes.

Our callers of that API log in with it: `hostctl.sync` and `hostctl.machine` through `units.anythingllm_headers`; a package that needs it uses `hostenv.anythingllm_headers`, which `units.anythingllm_headers` wraps. Each logs in once per process (a login lasts 30 days and is logged) and once more after a 401; with no password set they send nothing. The relay holds no key and doesn't log in: it checks each client's developer API key with `/api/v1/auth` and asks with that. `uv run hostctl health` fails when `/api/scheduled-jobs` answers without a login.

What a password doesn't close: `/api/request-token` has no rate limit, so the password has to be long; a developer API key (any client's, Nilson's included) still has full `/api/v1` access, `update-env` included; the agent's websocket needs only an invocation's id.

Not in this repo, so a new machine needs them first: rootless podman with Quadlet, systemd lingering for the user, HTTPS routes to the apps (see "The machine's routes"), uv, SearXNG (deployed by Ansible, see SearXNG below) and Ollama if it's the embedding provider. AnythingLLM's own settings (providers and keys in its `.env`, workspaces, which built-in skills are off) are set through its UI.

## Layout

- `anythingllm/agent-skills/<hubId>/` -- custom agent skills (`plugin.json` + `handler.js`)

  - `deep-research/` -- multi-source web research with GLM and DeepSeek, its report put in the workspace's documents; hands the work to `research-runner` on the host (see "Deep research")
  - `run-code/`, `write-file/`, `publish/`, `show-image/`, `build-site/` -- the code sandbox, run by `sandbox-runner` on the host (see "Code sandbox")
  - `browse/`, `browser-act/`, `browser-read/`, `browser-handoff/`, `browser-login/` -- the workspace's browser and its saved logins, run by `browser-runner` on the host (see "Browser")
  - `update-prompt/` -- refreshes the calling workspace's EverythingLLM block in its system prompt (below), through `agents-runner`'s `update_prompt`; it shows what would change first and writes only with `apply`
  - `scheduled-jobs/`, `schedule-job/`, `remind-once/` -- list, delete or disable AnythingLLM's scheduled jobs, make a recurring one, and set one-off jobs that are deleted once they've run, through `agents-runner` (see "Scheduled jobs from a chat"); each shows first and acts only with `apply`
  - `memories/` -- list, save or forget AnythingLLM's saved memories, through `agents-runner` (see "Saved memories"); each acts at once, and forget gives the text back
  - `_lib/` -- what the skills share (no `plugin.json`, so AnythingLLM doesn't load it as a skill): `hostrpc.js`, the node side of `packages/hostrpc`; `sandbox.js`; `browser.js`; `scope.js`, a call's {workspace, thread} for the two; `runner.js`; and `delegated.js`, the check that makes every skill of ours that writes, acts or delegates refuse a call from an `agents-*` workspace, where delegated tasks will run (`docs/.proposals/agents.md`). A test holds every skill to it; elsewhere, chats, the Nilson relay's API chats and scheduled jobs, nothing changes.

- `anythingllm/env.example` -- keys used in the live `.env` (values stay out of git)

- `anythingllm/system-prompt.md` -- the system prompt for chat and the agent: which tool to reach for, the tool-call budget, safety rules. A workspace's prompt is AnythingLLM's, edited in its UI, and deploy never writes one: ours goes in as a block marked with its version (`hostenv.prompt`), which deploy sets as the default for new workspaces. Deploy also keeps the System Prompt Variable `{everythingllm_version}` at the repo's version; the block asks the model to tell the user once when it's behind, and the `update-prompt` skill refreshes it, keeping the workspace's own text around it. `uv run hostctl health` lists the workspaces whose block is behind or missing. Scheduled jobs have no workspace, so they get AnythingLLM's built-in prompt instead; their own prompts carry what they need.

- `packages/` -- the host services, the MCP gateway and their libraries: members of the uv workspace at the repo root (`pyproject.toml`, `uv.lock`), one per subdirectory

  - `packages/sandbox/` -- not an MCP server: `sandbox-runner` runs the agent's Python and bash in throwaway podman containers on the host, with only PyPI on the network, and publishes pages from them, for the `run-code`, `write-file`, `publish`, `show-image` and `build-site` skills (see "Code sandbox" below). The Zola theme the agent's sites can take is in `packages/sandbox/zola/themes/` (see "Building sites")
  - `packages/browser/` -- not an MCP server: `browser-runner` runs a Chromium per workspace in a hardened container, with a live card per chat and a take-over view, for the browse skills (see "Browser"); `browser.driver` runs in that container
  - `packages/hostrpc/` -- a library, not a server: how the skills and the MCP gateway talk to the services on the host (see "Services on the host" below)
  - `packages/hostenv/` -- a library, not a server, standard library only: this host and its AnythingLLM as the services and hostctl see them: where things are kept (`storage`, `data_dir`), a service's socket, the pages site's address, AnythingLLM's login (`anythingllm_headers`) and the system prompt's block (`hostenv.prompt`)
  - `packages/research/` -- not an MCP server: `research-runner` runs the deep-research skill's runs, in a service container of its own, and `research-run` runs one by hand (see "Deep research")
  - `packages/agents/` -- not an MCP server: `agents-runner` runs delegations, tasks done by AnythingLLM's own agents, and `agents-run` starts one by hand (see "Delegation")
  - `packages/runs/` -- a library, not a server: what research-runner and agents-runner share for long runs: run state with long-poll waiting and slots, the run log, live cards
  - `packages/publicweb/` -- a library, not a server: the HTTP client research uses, which refuses LAN, CGNAT (Tailscale's) and loopback hosts, and `publicweb.pages`, the page reader on it
  - `packages/chatimage/` -- a library, not a server: the pictures the host draws for the chat, which the agent shows as Markdown images: link cards for published pages (see "Code sandbox"), deep research's live progress cards, and the server push that keeps a live one current (see "Deep research")

- `packages/egress/` -- the egress proxy, the service containers' only way out, and `egress.toml`, their addresses and what each may reach (see "Service containers")

- `packages/relay/` -- the Nilson relay, a service (in its own container) for the Nilson chat app rather than for AnythingLLM's agent; also a workspace member (see "Nilson relay")

- `packages/gateway/` -- the MCP gateway, a host service that serves the runners' tools over HTTP to MCP clients other than AnythingLLM (see "MCP gateway")

- `host/systemd/` -- host user units, rendered into `~/.config/systemd/user/` (`uv run hostctl units`); each one's `Description=` says what it does, and its app's `uv run hostctl <app>-setup` (see "The apps") enables it.

- What only host services read or write lives in `~/.local/share/everythingllm` (`hostenv.data_dir()`), not in AnythingLLM's storage, which the container mounts. It's laid out by kind:

  ```text
  venvs/<name>/        the host services' venvs (agents, browser, gateway, sandbox)
  venvs/<x>-ctr/       a service container's venv and uv cache (venv/, uv-cache/):
                       egress-proxy, relay, research-runner
  pages/public/        the pages site Caddy serves: link cards (_cards/), shown images
                       (_images/) and the research site's old reports (research/)
  sandbox/workspaces/  the sandbox's folders, one per workspace (threads/, project/,
                       shared/)
  sandbox/access.json  each workspace's web and model access (sandbox-access)
  sandbox/apps/        each app's write-back token (<workspace>/<name>.json, mode 600)
  sandbox/models/      the model calls runs made (YYYY-MM.jsonl), and sandbox/m/ their sockets
  browser/             browser-runner's: each workspace's browser profile
                       (profiles/<workspace>/), each running browser's sockets
                       (sockets/<slot>/), downloads as they're saved (downloads/), noVNC
                       for the take-over view (novnc/) and the saved logins
                       (vault/<workspace>.vault, sealed)
  research/runs/       the deep-research run log and live runs' markers
  agents/runs/         the delegations' run log and live runs' markers
  relay/               the Nilson relay's database
  hostctl/skills/      what the UI set (on/off, setup values) in each skill deploy took
                       out, for when its app is set up again
  ```

  Storage keeps AnythingLLM's own data, the runners' sockets (`storage/everythingllm/<name>/runner.sock`, which the container reaches) and what AnythingLLM reads (`anythingllm-fs/research/`, `documents/`).

- The `static_agent` Caddy container mounts just `pages/public/` read-only and serves it on 127.0.0.1:8445

- `host/quadlet/` -- the Quadlet units, as templates (`uv run hostctl units`): AnythingLLM, the pages site and the service containers

- `host/containers/` -- the images we build: the sandbox's (`uv run hostctl sandbox-images`), the service containers' (`uv run hostctl service-images`) and the workspaces' browser (`uv run hostctl browser-images`)

- `host/caddy/pages.Caddyfile` -- the pages site's Caddy config, including its CSP

- `packages/hostctl` -- `uv run hostctl <command>`, everything that sets up, syncs and checks the host (`cli`, the commands; `uv run` installs it into the dev venv first, so a fresh clone needs only uv). Standard library only, so `health.sh` and the apps' `before` steps run its modules with any `python3`.

  - `sync` -- diff/deploy/import between this repo and live storage
  - `units` -- renders and installs `host/quadlet/` and `host/systemd/` (`uv run hostctl units`)
  - `machine` -- `uv run hostctl install`'s checks, its wait for AnythingLLM, the web search setting and the closing checklist
  - `appctl` -- the apps' setup, logs and routes, from the registry
  - `run_guard` -- asks before a runner with a live run restarts
  - `agents_env`, `relay_env`, `gateway_env` -- the agents, relay and gateway setups' key file checks; `gateway_env` also adds a gateway client (`uv run hostctl gateway-client`)
  - `ctr_env` -- a service container's share of AnythingLLM's `.env`, which its template's `ExecStartPre` writes before each start
  - `health.sh` -- `uv run hostctl health`

## Workflow

`uv run hostctl` lists every command. Day to day: `uv run hostctl diff` shows what would change live, `uv run hostctl deploy` copies it into storage and restarts AnythingLLM, `uv run hostctl test` runs every test and `uv run hostctl health` checks every unit, port, host service and runner socket. `uv run hostctl import-skill <hubId>` brings a skill made in the UI under the repo. Slash commands aren't in the repo: they're AnythingLLM's, made and changed in its UI.

Skill handlers are re-required on each load, so a changed skill doesn't need a restart, but `uv run hostctl deploy` also runs `uv run hostctl restart`, so AnythingLLM picks up a new or removed skill and what it preloads (`thread-scope.js`, `agent-stop.js`, the log filter). Deploy copies only the skills of the apps set up here (see "The apps"). It doesn't remove a skill the repo dropped; delete its folder in `storage/plugins/agent-skills/` by hand. On deploy, a skill's `active` flag and any setup_args `value` saved through the UI are kept from the live `plugin.json` unless the repo sets a `value` itself. AnythingLLM's MCP servers (`storage/plugins/anythingllm_mcp_servers.json`) and scheduled jobs are its own: deploy writes neither.

## uv cheatsheet

The repo root is a uv workspace; each `packages/<name>/` is a member with its own dependencies and console scripts, all locked together in `uv.lock`. Run these from the repo root; the dev venv is `.venv` there, which is the interpreter `.vscode/settings.json` points at.

```sh
uv sync --all-packages                     # install every member + dev deps into .venv
uv run --all-packages --all-extras pytest -q   # all tests (what `uv run hostctl test` runs)
uv run --package sandbox pytest packages/sandbox -q   # one member's tests
# The tests that build a real site run zola in the sandbox image (the only zola there
# is), through podman; they skip without it (uv run hostctl sandbox-images).

uv add --package research httpx            # add a dependency to one member
uv add --package research --dev pytest-cov # ...or to its dev group
uv remove --package research httpx
uv lock                                    # re-lock after editing a pyproject.toml
uv lock --upgrade-package mcp              # bump one dependency
uv tree --package research                 # what a member pulls in
```

- `--package <name>` picks the member whose dependencies and scripts to use; the workspace root has no project of its own.
- `--frozen` uses `uv.lock` as-is and never re-locks; the host units and service containers use it so a running service never rewrites the lock.
- `--no-dev` leaves out dev groups. Don't combine it with `.venv`: uv syncs the venv to match, so it uninstalls pytest. Point `UV_PROJECT_ENVIRONMENT` at another venv instead, as the host units do.

## Services outside AnythingLLM

The repo is mounted read-only into the AnythingLLM container at `/mcp` (see the `Volume=` line in `host/quadlet/anythingllm.container.in`), for what it preloads (`thread-scope.js`, `agent-stop.js`, the log filter) and the skill tests. AnythingLLM runs no MCP server of ours: its MCP servers (`storage/plugins/anythingllm_mcp_servers.json`) are its own, set up in its UI. Its tools from this repo are the skills, and other MCP clients get the runners' tools over HTTP from the gateway (see "MCP gateway").

### Services on the host

Work that is heavy, long or needs the host goes to a service outside AnythingLLM, with the skill in the container as a thin front: `sandbox-runner` (the code sandbox), `browser-runner` (the workspaces' browsers) and `agents-runner` (delegation) as host units, and `research-runner` (deep research) in a service container of its own (see "Service containers"). Each listens on a Unix socket in storage, `storage/everythingllm/<name>/runner.sock` (mode 0660), which the container sees without a Quadlet change, and they all speak `hostrpc`'s protocol: one request per connection, a line of JSON each way, `{"op", "args"}` in and `{"ok": true, "result"}` or `{"ok": false, "error"}` out.

- `hostrpc.Service(ops, errors=…)` dispatches each request to the function of that name in `ops` (a package's `tools.OPS`) or to an `op_<name>` method of a subclass (research, sandbox), running one that isn't a coroutine in a thread; a `hostrpc.RunnerError` becomes the error the caller sees, as does that of the service's own `errors` (the sandbox's `SandboxError`); anything else is logged and reported as `runner error: …`. Every service answers `ping`, which `uv run hostctl health` asks. `hostrpc.serve` serves one on its socket and removes the socket on SIGTERM, or when a `stop` event is set (a runner's `main()` serves it on `hostenv.socket_path(<name>, <NAME>_SOCKET)`), and `hostrpc.serving` serves one for the length of a test.
- `hostrpc.request(socket, op, args, timeout, name=…)` asks one, raising `RunnerError` (also when nothing listens). A gateway front gets its `call(op, args)` from `hostrpc.caller(folder, env, name, error=ToolError)`, which turns that into a tool error, and `hostrpc.forwarder(call, mcp.add_tool)` makes each tool from a signature and docstring alone: calling it sends every argument as the op of its name. The skills speak the same protocol from node (`anythingllm/agent-skills/_lib/hostrpc.js`; `_lib/runner.js`'s `forward` for a skill that sends one op).
- An MCP client gives up on a tool call after a minute or so, so an op answers within 45 s, and work that takes longer carries on in the service (a run id to wait on) or in a unit of its own.
- The container maps the host user (`UserNS=keep-id`), so what a service writes in storage is the container's to read and the other way round, and file locks work across both.
- A new one: a `hostrpc.Service` and a `main()` that serves it with `hostrpc.serve` on `hostenv.socket_path` (`packages/research` is an example), a `<name>-runner` console script, a unit `host/systemd/<name>-runner.service` with its own venv in `~/.local/share/everythingllm/`, and an app `<name>` in `apps.toml` with `runner` naming that unit (see "The apps"). Its socket is `storage/everythingllm/<name>/runner.sock` (`hostenv.socket_path`), which the gateway hands its front. A runner may be a container instead (see "Service containers"): its `runner` is then the container's `<x>.service`.

A runner runs the code it started with: a code change goes live when it restarts (`uv run hostctl <app>-setup`). Note that this runs whatever is in the working tree, committed or not.

The machine routes HTTPS :8445 to the pages site and :8447 to the workspace pages site (see "The machine's routes").

### Service containers

A host service can run in a container of its own instead of as a host unit, hardened like the sandbox's containers and with one way out, the egress proxy. Each service moves over on its own: its template goes from `host/systemd/<x>.service` to `host/quadlet/<x>.container.in`, and its app's `runner` and journal key follow (`apps.toml`'s `container`, `systemd-<x>`). The next `uv run hostctl units` retires the old host unit: it stops, disables and deletes its installed copy (systemd prefers `~/.config/systemd/user/<x>.service` to the unit Quadlet generates under the same name), then starts the container; a guarded runner with a run going is left for a later run. It retires a container whose template is gone the same way, before it writes anything: it stops `<x>.service` (a Quadlet unit can't be disabled) and deletes the `.container`, and the reload drops the unit. It never retires `anythingllm.container` or `egress-proxy.container` (everything goes with them), and says so; one it didn't render, or a link, it leaves alone. `uv run hostctl diff` lists what it would retire. So far the relay and research-runner have moved; the old venvs in `venvs/<name>/` can go once their containers work.

**The image.** Every service container runs `localhost/everythingllm-service` (`host/containers/service/Containerfile`): `python:3.12-slim`, the host's uv copied from its own image, tzdata, the DejaVu and Liberation fonts chatimage draws with, and CA certificates. It holds none of our code. `uv run hostctl service-images` builds it and creates `egress-net`; the `egress` app's setup runs it first.

**The paths are the host's.** The repo is mounted read-only at its own path (`@REPO@`), and the container runs `uv run --frozen --no-dev --project @REPO@ --package <pkg> <script>` with `HOME=%h`. Everything else it mounts (its folders in the data dir and in storage, its socket folder) is mounted at its host path too, so a path means the same inside and out: what a runner tells the container, what lands in a run log, the report file research names in its run log for agents-runner. `host.env` comes in through `EnvironmentFile=`, as does an app's own secrets file (`relay.env`): podman reads them on the host and passes the values in, so they aren't mounted. It takes each value as it is, quotes included, and the template's own `Environment=` lines win over both. AnythingLLM's `.env` is never mounted: it holds every provider's key, the password and the signing secrets. A container that needs a key of it gets its share instead, a file with just those keys that `hostctl.ctr_env` writes on the host before every start (the template's `ExecStartPre`), in `~/.config/everythingllm/ctr/<x>.env` (mode 600), mounted read-only and named by `ANYTHINGLLM_ENV`. The template lists the keys; a key whose value isn't needed, only whether it's set (`JWT_SECRET`, by which `hostenv.anythingllm_headers` knows the password is on), goes in as `set`. A key changed in AnythingLLM's settings reaches a container at its next restart. Each container has one folder of its own, `~/.local/share/everythingllm/venvs/<x>-ctr/`, with its venv (`UV_PROJECT_ENVIRONMENT=…/venv`) and its uv cache (`UV_CACHE_DIR=…/uv-cache`) in it: one mount, so uv can hardlink, and no container can touch another's packages. The first start syncs the venv from PyPI through the proxy (a minute or three); later ones find it synced.

**A code change reaches a container by a restart**, as it does a host unit: `uv run hostctl <app>-setup`, or `systemctl --user restart <x>.service`. `<app>-setup` restarts an app's containers (it doesn't enable them: Quadlet's `[Install]` does), asking first while a guarded runner has a run going, as for a host unit. `uv run hostctl units` starts a changed container, except a guarded one with a run going, and one whose image, network or egress proxy isn't there yet, which waits for its app's setup (or `units egress`). The egress proxy is guarded by research's runs, since its restart cuts their requests.

**Going back to a host unit** is done by hand. Stop `<x>.service` and move `~/.config/containers/systemd/<x>.container` out of that folder first: a host unit of the same name in `~/.config/systemd/user/` takes the name over at the next reload while the container still runs (`uv run hostctl units` does the same when it finds the template gone, but by hand nothing else has changed yet). Get the old host unit from git (`git log --diff-filter=D -1 --format=%H -- host/systemd/<x>.service` names the commit that dropped it, `git show <hash>^:host/systemd/<x>.service` prints it) and bring it up to date: it predates whatever changed since, research's for one has neither `ANYTHINGLLM_ENV` nor `SEARXNG_URL` and names the old venv. Put it back in `host/systemd/`, delete `host/quadlet/<x>.container.in` and run `uv run hostctl units`. Then `systemctl --user enable --now <x>.service`: `<app>-setup` enables only an app's `units`, and `hostctl deploy` gives an app its skills only while its runner is enabled. `health` and `<app>-logs` follow the container (`apps.toml`'s `container`) until the app's entry is moved back too.

**Hardening.** Every service container's template has these Quadlet keys (`packages/egress/tests/test_quadlet.py` holds them to it):

```ini
Image=localhost/everythingllm-service
ReadOnly=true                 # the root filesystem; /tmp is a tmpfs
Tmpfs=/tmp
DropCapability=ALL
NoNewPrivileges=true
UserNS=keep-id                # it runs as the host user, so files and sockets are theirs
GroupAdd=keep-groups          # only one that writes in storage: the anythingllm group
PidsLimit=256
PodmanArgs=--memory=<n> --cpus=<n> --umask=0002
RunInit=true                  # a PID 1 that passes on SIGTERM
Timezone=local
Network=egress-net:ip=<its address in egress.toml>
Environment=HTTPS_PROXY=http://10.89.79.2:3128   # and HTTP_PROXY
Environment=EGRESS_PROXY=http://10.89.79.2:3129  # the public port, for public_client
PublishPort=127.0.0.1:<port>:<port>              # one with an HTTP port
```

Quadlet in podman 5.4 has no `Memory=` or `Umask=`; `PodmanArgs` carries them, and `hostctl`'s tests convert every template with `/usr/libexec/podman/quadlet -dryrun`, which refuses a key it doesn't know. A template never sets `ContainerName=`: Quadlet's `systemd-<x>` is the name `<app>-logs` finds its journal by. Inside, the `anythingllm` group shows as `nogroup` (65534): access through it works, but code can't chgrp to it or look it up by name; storage's setgid folders give new files the group anyway.

**Ports and addresses.** A service's HTTP port is published on the host's `127.0.0.1`, so `apps.toml`'s `serve` and health checks are unchanged. What comes through arrives from the container's own address, not its loopback, so a server in a container listens on `0.0.0.0`: `LIVE_HOST` (the live cards, `runs.live`) and `RELAY_HOST` (the relay) say so in its template, and default to `127.0.0.1` on the host. For the same reason a server that believes the machine's route's `X-Forwarded-For` and `X-Forwarded-Proto` believes them from its container's own address, not `127.0.0.1`: the relay's template sets uvicorn's `FORWARDED_ALLOW_IPS` to it. Listening on `0.0.0.0` would also let every other container on egress-net reach that port (podman's bridge doesn't keep them apart), and a container that a page had taken over could act through them. So each of these servers answers a connection only from loopback or its own address, the socket's local one (`hostrpc.local_peer`; the relay's `LocalPeers` runs it outside uvicorn's proxy headers, which would put the forwarded client in the peer's place), and refuses any other with a 403: it works unchanged as a host unit on `127.0.0.1` and behind the published port, and another container, coming from an address of its own, gets nothing. A container can't reach the host's loopback either, so it reaches AnythingLLM and SearXNG by `PUBLIC_HOST` through the proxy: `ANYTHINGLLM_URL=https://<PUBLIC_HOST>:3001` (the relay) and `SEARXNG_URL=https://<PUBLIC_HOST>:8888/search` (research). All default to the host's loopback.

**The egress proxy** (`packages/egress`, the `egress` app) is egress-net's only way out. `egress-net` is an internal podman network (`10.89.79.0/24`), with no route and no DNS. podman gives a container that names no address one from `10.89.79.128/25` (`ip_range`), apart from every service's, so a stray one can't take a stopped service's address and its profile. `egress-proxy` runs in a container of the same image, on egress-net at `10.89.79.2` and on podman's default network for its own way out, and listens at `10.89.79.2:3128`, and at `:3129`, its public port (below). It takes `CONNECT host:port` (https) and absolute-form plain-http requests, and judges each by the caller's address on egress-net and the host and port asked for:

- `packages/egress/src/egress/egress.toml` gives each container its address (`ips`) and each service a profile: `public` (any host whose addresses are all public, on ports 80 and 443) and `allow`, `host:port` exceptions reached whatever their address. Every profile also allows `pypi.org:443` and `files.pythonhosted.org:443`, for uv. `@PUBLIC_HOST@` and `@NTFY_HOST@` (default `ntfy.sh`) come from the proxy's environment.

| profile | containers (address) | public | allow |
| --- | --- | --- | --- |
| relay | relay (.10) | no | `PUBLIC_HOST:3001`, ntfy :443 |
| research | research-runner (.11) | yes | `PUBLIC_HOST:8888`, ntfy :443 |
| browser | the workspaces' browsers (.32--.35, one per slot) | yes | none |
| sandbox | the code sandbox's runs (.40--.41, one per slot) | no | none (PyPI, as every profile) |
| sandbox-web | the runs of a workspace with web access (.42--.43, one per slot), on the public port alone | yes | none |

- A public host must resolve to public addresses only, all of them: the rule is `publicweb.public_address`, the one the services use on the host, so loopback, the LAN, link-local, the CGNAT range (Tailscale's) and IPv4-mapped forms of them are all refused. The proxy resolves each name once and connects to the address it checked, so a name that answers differently the second time (DNS rebinding) gets nowhere.

- Anything else is refused with a 403 that says why, as is a connection from an address no profile has. Each connection logs its profile, method, `host:port` and verdict, never a path or a query (`uv run hostctl egress-logs`).

In a container, `EGRESS_PROXY` puts `publicweb.public_client` in proxy mode: every request goes to the proxy, which makes the address check, and the client checks only the scheme. It names the proxy's public port, `:3129` (`public_port` in egress.toml), where only `public` counts and no `allow` exception does, PyPI's included: `public_client` fetches URLs that came from the web or the agent, and on the host it refuses CGNAT and the LAN, so a page or a redirect mustn't reach AnythingLLM or SearXNG through the container's exceptions either. Other clients (the services' own httpx clients for AnythingLLM, SearXNG, DeepSeek and ntfy, and uv) follow `HTTPS_PROXY` and `HTTP_PROXY`, on `:3128`. On the host none of these is set, and nothing changes.

## Code sandbox

Seven agent skills give the agent a small Linux machine to run code in, like the Claude app's, ways to show and publish what it makes, apps it keeps from templates, and a switch for what it may reach:

- `run-code` runs a Python or bash script and replies with its output. It waits for the whole run (up to 300 s), showing in the chat that it's still going; skills, unlike MCP tools, have no 60 s limit. Reading, listing, moving and deleting files is bash.
- `write-file` writes a text file, or deletes a file or folder (deleting exactly one of the workspace's folders empties it).
- `publish` gives a page's link and card, lists the workspace's pages, copies a file or folder from elsewhere into `/public`, or removes a page.
- `show-image` shows an image file from the sandbox in the chat (see "Images in the chat").
- `app` keeps the workspace's apps, lists first: one call makes a list, adds, ticks off or removes items, and replies with the list's live card (see "Apps").
- `build-site` builds a Zola site from the workspace's folders into `/public/<slug>`, which puts it live (see "Building sites").
- `sandbox-access` shows whether the workspace's runs can reach the web and ask a model, and turns either on (once the user approves it) or off (see "Web and model access").

**Pages are `/public`, served as they are.** A workspace's `/public` is its pages on the web, at `https://<PUBLIC_HOST>:8447/<workspace>/`: `public/notes/index.html` is `/<workspace>/notes/`, and any other file is served as it is. Whatever is written there is live at once, and deleting it takes it down; there's no copy, no sync and no page names to claim, since each workspace owns its prefix. A half-written or broken page is the workspace's own business. The replies of `run-code`, `write-file` and `build-site` list the pages they changed, with their URLs, what in them the CSP blocks and their notices (scripts, and the sandbox's limits on them). Caddy's directory listing is the index, of the workspaces at the root and of a workspace's pages under it; dotfiles aren't served.

What keeps this safe is where `/public` lives: in `~/.local/share/everythingllm/sandbox/public/<workspace>/`, apart from the workspace's other folders, in a tree that holds nothing but `/public` folders. Caddy mounts that tree read-only, so a symlink in a workspace's pages can only reach other workspaces' pages (public already) or Caddy's own container, never a workspace's `/project`, `/work` or `/shared`. `/public` counts toward the workspace's size limit.

**Link cards.** AnythingLLM's chat shows a Markdown image up to 800 px wide, and keeps it a link when it's inside one, even with "Render HTML in chat" off. That setting is per browser and off by default, and the HTML it lets through is sanitized (DOMPurify: no scripts, handlers or iframes), so anything richer than text that has to show wherever the chat does is a picture the host draws: `packages/chatimage`, whose link cards are one kind and deep research's live progress cards another. `publish` has `chatimage.card` draw a card of a page (its title, its workspace, a line about it, its address) into `_cards/` on the pages site, and adds a `Card: [![title](card.png?v=…)](page)` line to its reply, which the system prompt has the agent paste as is, and link a page through rather than from memory. The `?v=` is a hash of what the card says, kept in the PNG too, so an unchanged card isn't redrawn and a changed one gets a new URL the chat hasn't cached. Removing a page deletes its card.

**Apps.** An app is a template from the repo plus a workspace's data for it (`docs/.proposals/sandbox-apps.md`): the agent changes the data in one call to the `app` skill, and the host renders the page, so no page is written by hand and every list looks and works the same. Templates are in `packages/sandbox/src/sandbox/apps/` (a list is the first: `list/template.py`, its ops and its card, and `list/page.html`), reviewed with the runner, never changed by a workspace (a workspace's own templates are a TODO).

- An app's data is `/project/apps/<name>/data.json` and its page `/public/apps/<name>/` (at `https://<PUBLIC_HOST>:8447/<workspace>/apps/<name>/`). The runner's `app` op (`create`, `do`, `show`, `list`, `delete`) reads the data without following a symlink and checks it against the template (a run may have edited it), applies one of the template's ops, and renders the page: the template's page with the data embedded as JSON, `<`, `>` and `&` escaped, drawn by its own script with `textContent`, so no HTML is built from what the data says.
- Its card is live and per app: `https://<PUBLIC_HOST>:8445/_live/apps/<workspace>/<name>.png`, linking to the page, served by the sandbox runner's apps server on 127.0.0.1:8455 (`sandbox.appsweb`, `APPS_PORT`). It pushes a new frame whenever the app changes, through the op or by a run's edit to its data (looked at every 2 s), for 30 minutes a view; an old chat's card shows the app as it is now, and a deleted app's says so.
- The page saves what the user does: it posts the template's ops to `/_apps/<workspace>/<name>/ops` on its own host, which the machine routes to the same server. Pages run in an opaque origin, so it posts `text/plain` and the answer allows origin `null`; `default-src 'self'` already lets a page connect to its own host. What makes a post the page's is its token: each render makes a new one (kept host-only in `sandbox/apps/` in the data dir, mode 600), a page rendered before gets 409 ("reload"), anything else 403. At most 4 KB a post and 10 ops in 10 s per app, none while a run holds the workspace, and the answer carries the next token, so the page goes on without reloading. `packages/sandbox/tests/test_pages_browser.py` checks the post in a real Chromium. App pages are served with `Cache-Control: no-cache`, and both pages sites with zstd or gzip.

**Images in the chat.** `show-image` puts an image the sandbox has (a chart `run-code` saved, a photo in `/project`) in the chat, as a Markdown image in a link to itself, on an `Image:` line the agent pastes as it does a card. The runner (`op_show_image`) reads the file from the caller's own folders without following a symlink, at most 10 MB, and takes it only if Pillow reads a PNG, JPEG, GIF or WebP header in it (nothing is decoded); an SVG is refused, since opened by itself it's a page, so the agent converts one with `run-code`. It copies the file to the pages site's `_images/<workspace>/`, named by a hash of its bytes and its format: the address can't be guessed, the same image keeps its address, and a changed one gets a new one the chat hasn't cached. They're served under the pages site's CSP and `nosniff`, with no CORS header, since a picture can show anything the workspace has: a page on another origin can show one in an `<img>`, but not read it. The images aren't in the workspace's folders, so they don't count toward its size limit; past 500 MB a workspace's least recently shown go, and old chats show them broken.

**Card themes.** Every card the host draws, link card or live, comes in a dark and a light theme (`chatimage.THEMES`, with a 2 px outline so a card stands off a background of its own colour). It's dark unless its address asks for light with `theme=light`, so AnythingLLM's chat always shows the dark one, and a client in a light theme (the Nilson app) adds `theme=light` to each card's image address: `…/_cards/<name>.png?v=…&theme=light`, `…/_live/research/<id>.png?theme=light`. A link card is saved twice, `<name>.png` and `<name>.light.png`, and the pages site's Caddyfile serves the light one for `theme=light`, or the dark one for a card drawn before there were two; the live cards draw the theme they're asked for (`chatimage.live.theme`).

**Cards from another origin.** A client's web build (the Nilson app's) fetches the cards to draw them, from a page on an origin of its own, so card images say `Access-Control-Allow-Origin: *`: the link cards by the Caddyfile's `@cards` rule, the live ones by `chatimage.live` (images only, never a card's link page or redirect). It's asked for without credentials, so nothing more is needed. A browser tab's card is the exception: it's a screenshot of whatever the tab is logged into, and a page that learned its address (from the agent, say, talked into it by a page) could read it, so it says nothing of CORS. A web client with a developer API key draws it from the chat's `card.jpg` instead ("A chat's browser for a client app"); a native client isn't held to CORS and draws it.

Code never runs in the AnythingLLM container, which has SYS_ADMIN, the `.env` keys and all of storage. The skills (`anythingllm/agent-skills/`, sharing `_lib/`) only forward calls over a Unix socket, `storage/everythingllm/sandbox/runner.sock` (see "Services on the host"), to `sandbox-runner` on the host (`host/systemd/sandbox-runner.service`, its own venv in `~/.local/share/everythingllm/venvs/sandbox`).

**Scopes.** A call carries where it came from, which AnythingLLM gives the skill and the model never chooses: the workspace (`_jobs` for a scheduled job, which has none) and the chat thread (`default` for a workspace's main chat, an API chat with no thread and scheduled jobs, which have none). AnythingLLM 1.16.2 knows an API or Telegram chat's thread but leaves it out of the invocation it gives skills, so `anythingllm/thread-scope.js`, preloaded into its server beside the log filter, puts it in as `ephemeral.js` loads (the upstream fix, drafted as an issue; `uv run hostctl health` says when the patch stops fitting or isn't needed any more). Without it, all of a workspace's API chats, the Nilson app's included, would share one scope. A call through the MCP gateway carries the workspace `client-<name>`, from the client's token, the thread `gateway` and `gateway: true` (see "MCP gateway"). Workspaces whose names start `client-` are kept for those: the runner refuses one in a scope that doesn't say `gateway`, so an AnythingLLM workspace slugged `client-…` gets a message to rename it rather than a gateway client's folders. Each run mounts:

- `/work`: the thread's scratch folder, and where a run starts. It's deleted 7 days after the thread last used the sandbox.
- `/project`: the workspace's folder, shared by its threads and kept until deleted. `pip install`s go to `/project/.local`, so they last too. To keep a file, move it here.
- `/shared/<workspace>`: what the workspace shares with the others, kept until deleted. It writes it; every other workspace's runs mount it read-only at `/shared/<that workspace>`. Nothing is written by more than one workspace, so a prompt injection in one chat can't change what other workspaces use; reading another workspace's folder is still trusting its content, which the skills and system prompt tell the agent to treat as data. Shared folders are mounted `noexec,nosuid,nodev` and are never on `PATH`.
- `/system/themes`: the repo's Zola themes (`packages/sandbox/zola/themes`), read-only, for sites the agent builds.
- `/public`: the workspace's pages on the web (see "Pages are `/public`").

**Chat attachments.** A file attached in an AnythingLLM chat is in that chat's `/work/attachments/` as text, for `run-code` to read rather than the agent pasting it into a script. AnythingLLM keeps no attached file, only the text it made of it (`storage/direct-uploads/<name>-<uuid>.json`, and a row in its `workspace_parsed_files` table), so `.csv`, `.tsv`, `.txt`, `.md` and `.json` keep their names and anything else becomes `<name>.txt` (a PDF's text; a spreadsheet's sheets as CSV, their names in the file's). `run-code` looks the chat's attachments up in AnythingLLM's database, through the server's own Prisma client (`_lib/attachments.js`: the skill runs in AnythingLLM's server), and sends the runner their titles and file names, at most 50. API, Telegram and job runs have no chat and send none; the gateway's `sandbox_run` never does, and the runner ignores them for a gateway scope. Before the run, under the workspace's lock, the runner reads each new one from `SANDBOX_UPLOADS` (storage's `direct-uploads`) without following a symlink, at most 50 MB a file and 200 MB a run, and writes its text; `.manifest.json` beside the copies records each one's source and hash. A copy already there stays as it is, edited or not. When the lookup was whole, a copy of a file no longer attached is removed if it's unchanged, and an edited one stays as the chat's own; a failed lookup removes nothing, and neither does an attachment whose text is gone. Copies count toward the workspace's limit (past it, they're left out with a note), aren't among the files a run changed, and the reply names them.

They live in `~/.local/share/everythingllm/sandbox/workspaces/<workspace>/` (`threads/<thread>/`, `project/` and `shared/`), out of the container's reach. The runner's own file operations (`write-file`, `publish`) only take paths in the caller's own folders, never another workspace's. A workspace's folders together are held to 5 GB: over that, runs and writes are refused until the agent deletes something with `write-file`, and the refusal names the biggest files and folders, since no run can look for them. A run warns past 4 GB, and one that takes the workspace past 6 GB or 200,000 files and folders (hidden ones too) while it goes is killed (the runner looks every 3 s), so a run can't fill the host's disk. Runs in one workspace take turns, since they share `/project`; while one is going, a write or publish from any of the workspace's chats fails at once rather than waiting. Runs in different workspaces overlap. `docs/.proposals/shared-sites.md` (kept out of git) has the design.

**The lab site** is the one site the agent controls entirely: templates, stylesheets, `zola.toml` and content, in education's `/shared/education/sites/lab/`, where other workspaces can read it and copy it. It started as a copy of the `agent-site` theme and a welcome entry, with a `README.md` for the agent and a git repository so it can roll back. It's built with `build-site` (`path` `/shared/education/sites/lab`, slug `lab`), which puts it at `https://<PUBLIC_HOST>:8447/education/lab/`. Nothing in the repo or on the host reads it, so it can break without breaking anything else, and the CSP and its sandbox still hold for whatever it serves.

**Building sites.** `build-site` (`op_build_site`) builds a Zola site from a folder in the workspace's own `/project`, `/shared/<workspace>` or `/work` (the folder's name is the slug unless one is given). The build runs `packages/sandbox/src/sandbox/sitebuild.py`, copied from the repo into the run's read-only `/sandbox`, so nothing in a workspace's folders can change what a build runs, in a container with no network at all and every folder read-only but an empty `/out`:

- it copies the site to `/tmp`, leaving out `.git` and an old `public/`;
- it puts the theme named in `zola.toml` in place: with `[extra.build] theme_from = "system"` the repo's from `/system/themes`, with `theme_from = "<workspace>"` that workspace's `/shared/<workspace>/themes/<theme>`, and without it the site's own `themes/`. Another workspace's theme comes in without its symlinks, and can't itself be one: zola copies static files through a symlink, so a theme's `static/x -> /project` would otherwise publish the building workspace's private files (zola already keeps `load_data` inside the site);
- it runs `zola build` with the base URL the runner passes in (`…:8447/<workspace>/<slug>`), so a site can't point its links at another host, within 60 s.

The runner copies the output into `/public/<slug>` (plain files only, in place of what was there), so a site is live like any page; zola's error comes back if it doesn't build, and nothing changes then. A build waits like a run (`op_wait`).

**Each run** gets a fresh `localhost/everythingllm-sandbox` container, with the script mounted read-only from a host-only folder at `/sandbox`:

- non-root (`--userns keep-id`), read-only root, `--cap-drop ALL`, `no-new-privileges`;
- 1 CPU, 1 GB memory, 256 processes, 4096 open files, no file over 2 GB, 60 s by default (300 s max), then killed; a run that hits the memory limit is reported as such (podman's `OOMKilled`);
- output clipped to the first and last part; at most 2 runs at once;
- containers carry the label `everythingllm-sandbox=1`; the runner removes any left over from a crash or restart when it starts.

The host never follows a symlink out of a mount when it reads, writes or copies for the agent, won't write into a FIFO or device there, and leaves symlinks out of what `publish` or a build copies into `/public`; the sandbox can create any symlink it likes in its own folders.

**Network.** A run sits on `egress-net` (see "Service containers"), with no route out and no DNS (`--dns none`), at one of the egress profile `sandbox`'s addresses (`10.89.79.40`, `.41`): each is a slot, held from writing the run's script until its container is removed, so at most two run at once and an address is never handed on while a stopped container still has it (a build, with no network, holds a slot too, for its turn). The runner points `http_proxy` and `https_proxy` at the egress proxy (`:3128`), whose `sandbox` profile has no `public` and no exceptions of its own, only what every profile may reach: `pypi.org:443` and `files.pythonhosted.org:443`. So `pip install` works, and the internet, the LAN, CGNAT (a tailnet's AnythingLLM API, Ollama, ...) and the host's own ports don't. `upload.pypi.org` stays blocked, so code can't push data out through a package upload either. To allow another host, add it to the profile's `allow` in `egress.toml` and restart `egress-proxy`.

**Web and model access.** A workspace's runs can reach the public web, and ask a model, once the user turns that on for the workspace. Both are off by default, and a gateway client's `client-*` workspace can have neither. The `sandbox-access` skill shows them and changes them, through the runner's `access` op, which keeps each workspace's settings in `~/.local/share/everythingllm/sandbox/access.json` (mode 600, outside every workspace's folders, so no run can write it), read at each run:

- Turning either off, or lowering the model budget, works from any chat. Turning either on, or raising the budget, works only from a chat in AnythingLLM's own window, and only once the user approves it in AnythingLLM's tool approval prompt (`requestToolApproval`), which says what it means. AnythingLLM answers "approved" without asking anyone for a scheduled job, a skill set to run without asking, and a channel with no prompt (an API or Telegram chat), so the skill takes only its "User approved the tool execution." and refuses the rest with the reason. The runner refuses to turn anything on unless the skill says the user approved.
- A run with web access takes an address of the egress profile `sandbox-web` (`10.89.79.42`, `.43`; its own two slots), which may reach public hosts, and goes out through the proxy's public port (`:3129`), where no exception counts: public hosts only, on 80 and 443, PyPI included, never the LAN, CGNAT or the host.
- It doesn't see other workspaces' `/shared` folders, since a run that reads the web could send whatever it can read to any website; its own folders are as ever. Its reply says that web access was on.
- What's left is the user's call, made when they approve it: a page a run reads could carry instructions for the agent, and a run could send the workspace's own files anywhere public.
- A run with model access gets a Unix socket of its own (`sandbox.models`), served by sandbox-runner for as long as the run lasts, in `~/.local/share/everythingllm/sandbox/m/<run>/` and mounted read-only at `/run/everythingllm` (`EVERYTHINGLLM_MODELS`), and a stdlib client copied into its `/sandbox` (`sandbox/model_client.py` as `everythingllm_models.py`): `from everythingllm_models import ask` in Python, `python3 /sandbox/everythingllm_models.py "prompt"` in bash. The socket is who's asking (one run's workspace and thread), so nothing the run sends says that. The runner asks the model through `packages/llm` with the key from AnythingLLM's `.env`, read on the host; no key goes into a run. Models are `deepseek-flash` (the default) or `glm-5.3`; an answer is at most 8,192 tokens, a request at most 200,000 characters, and a run has at most 4 calls going.
- Each workspace has a budget of model tokens a day, in and out, in the user's time zone: 200,000 unless set (`daily_tokens`, up to 10 million). A call past it is refused. Every call is a line in `~/.local/share/everythingllm/sandbox/models/YYYY-MM.jsonl` (when, the workspace and thread, the model and the tokens; never the text), which is where the budget is counted from. The run's reply says how many tokens are left.

`uv run hostctl health` checks the unit and pings the runner, which reports a missing image or network and an egress proxy that isn't running.

## Browser

Each workspace has a browser of its own, a real Chromium that the agent drives and you can watch and take over, like the browser in Meta's Muse but split by workspace: a login made in `career` is there for every chat in `career` and never for `education`. Four skills drive it:

- `browse` opens an address in this chat's tab and replies with the page as text: its interactive elements, each with a ref (`[e12] button "Sign in"`), then its visible text, under a line saying it's the page's own, untrusted content. The first time, it also gives the tab's live card.

- `browser-act` does one thing to an element by its ref (click, fill, type, press, select, check, hover, scroll, back, forward, reload, wait) and replies with the page after. `press` sends plain keys only (Enter, Tab, an arrow, a character, Shift with Tab or an arrow), never a Control, Meta or Alt shortcut, so nothing goes through the clipboard.

- `browser-read` reads the page again, or only its lines that contain `find`. With `card`, it also gives the tab's card for the reply, for when you ask to see the browser: `browse` gives the card only when a chat's tab is new, and a card scrolled out of sight was otherwise gone. It's given whenever the chat has a tab, closed, stopped or yours, with why the page can't be read in its place.

- `browser-handoff` gives you the browser (to log in, enter a 2FA code, solve a CAPTCHA, pay) and replies at once with the card. The agent puts the card in its reply and ends the reply, since a skill that waited would keep the card out of the chat. Until you hand the browser back, its actions are refused. Hand it back in the take-over view, or tell the agent you're done, and it calls `browser-handoff` with `done: true`.

- `browser-login` logs in with a login or passkey saved in the workspace's vault, without the agent ever seeing it, or asks you for a login on a card (see "Saved logins" below).

A page that is a bot check, or shows one, says so at the top of `browse`'s, `browser-act`'s and `browser-read`'s replies. Cloudflare's interstitial, known by its whole title ("Just a moment..."), comes with a note to hand the browser over now rather than reload: reloading through one cost a run two minutes. A Cloudflare (Turnstile among them) or hCaptcha challenge box big enough to be seen in a page of the site's own (`driver.bot_box`; sites load invisible ones everywhere) often passes by itself, so its note says to read the page once more and hand over if it's still there.

While it runs, each call says in the chat what it's doing (AnythingLLM's introspect line, which its own UI and the developer API's clients show), so the steps of filling a form don't all read `browser-act`: "Opening httpbin.org", "Filling in Customer name", "Clicking Submit order", "Handing the browser to you". An element goes by its name in the last read, which browser-runner keeps for the tab and gives the skill (`label`), else by its ref. What's typed or chosen is never in it, nor an address's path or query, nor a key but a named one.

They're skills, not MCP tools, because they act and must know their workspace: each call's scope is `{workspace, thread}` from AnythingLLM's invocation (`_lib/scope.js`, as the sandbox's), never from the model, and each refuses a delegated task. Gateway clients get no browser.

**The card.** A tab's card is a live picture of it in the chat: `https://<PUBLIC_HOST>:8445/_live/browser/<id>.jpg`, served by browser-runner on :8453 (`browser.live`, routed by the machine like the research cards). It's the tab's screenshot under a strip saying what's being done with it (the agent is browsing, while one of its steps runs and for 30 s after; the agent's but idle; waiting for you, after a handoff or while it waits for your OK or a login; you have it; or closed), which the take-over view says too, the page's title and address, and what was done last ("Clicked Sign in", by the element's name in the last read; what's typed is never shown). It's pushed again (`multipart/x-mixed-replace`, as JPEG) whenever the tab looks different, checked once a second while someone watches. A watched card keeps the browser from being stopped as idle. A closed tab shows its last look, dimmed, and the card wakes up when the tab is used again; the same chat keeps the same tab and card from one container to the next. The tab's id is `bw-` and 16 hex digits, so the card is the way to it. The card's line in the chat names the page by its title, or by its site (`origin.registrable`, or the host) when it has none or a bot check's, never by its address, whose path can hold a token.

**A chat's browser for a client app.** The agent puts a card in the chat only as its reply ends, if it does. A client with an AnythingLLM developer API key can ask instead: `GET https://<PUBLIC_HOST>:8445/_live/browser/chat/<workspace>/<thread slug>` (`chat/<workspace>` for the workspace's main chat, where a developer API chat sent without a thread goes) with `Authorization: Bearer <key>` answers the chat's tab (its card, where the card links, its state as the strip says it, the page's title as the card names it, and what was done last) and its requests for logins (card, link, site, and waiting, saving, saved, declined or expired), as JSON (`browser.chats`). The key is checked with AnythingLLM's `/api/v1/auth`, as the relay checks it, and never kept; the thread's slug becomes the id the tabs are kept by through AnythingLLM's internal API. So AnywhereLLM can show the card from the agent's first step and say who has the browser after the answer ends. The tab's entry names a `frame` too: the same route with `/card.jpg` (`chat/<workspace>/<thread slug>/card.jpg`, `chat/<workspace>/card.jpg` for the main chat) answers the key with the tab's card as it is now, one JPEG rather than a stream, which a client asks for again to follow it (`?theme=light` as on the card), or a 404 while the chat has no tab. This route answers any origin (CORS), since it answers only a key. The card's own address doesn't: it needs no key, and a page that read a tab's screenshots could read whatever the tab is logged into, so a web client reads the picture only through `card.jpg`.

**The take-over view.** The card links to `https://<PUBLIC_HOST>:8454/<token>/` (through a redirect from :8445, since the token changes with each container), a page of its own on its own HTTPS port (`browser.takeover`, :8454), so its scripts run on an origin of their own and not the pages site's. It shows the browser's whole screen through noVNC (`static/app.js`), view-only while the agent has it. Scaled to fit, the 1280 px screen can't be read or tapped on a phone, so below 800 px wide the view shows it at its own size and a drag pans it (a tap still clicks once it's yours); "Fit the screen" scales it to fit, and "Actual size" goes back. "Take over" makes it yours: the agent's actions and reads are refused (so it can't watch what you type) until you press "Hand back to the agent". While it's yours, a field under the screen, "Type into the browser", types what you send into the field in focus there as text (`POST /<token>/type`, the driver's `page.keyboard.insert_text`), not as key presses: a phone's keyboard, any keyboard layout, a paste and a password manager all work, where the canvas takes none of them. A second field is a password one (`autocomplete=current-password`, so a manager offers your login), and buttons press Tab, Backspace and Enter there (`POST /<token>/key`). What you send goes to the driver alone, never into a log, an answer or a card, and the runner refuses it unless the browser is yours. On a phone (narrow, or a touch screen) "Take over" focuses the field, so the keyboard opens, and "Keyboard" brings it back. When it comes back, whatever is in a password field, sent or not, typed over VNC or through the field, is hidden from its reads as a filled secret is, and so is a login you sent and whatever you sent from the password field. The VNC stream reaches the page over a WebSocket the runner carries to x11vnc's Unix socket (`browser.websocket`); nothing in the container listens on a port. A POST or a WebSocket must come from the page's own origin. noVNC's files come from the browser image (`uv run hostctl browser-images` copies `/opt/novnc` to `~/.local/share/everythingllm/browser/novnc/`), so the page and the image's x11vnc are from one build.

**Saved logins.** Like Muse's credential vault, the workspace's logins are the agent's to use and never to read. They live in browser-runner, outside both the agent and the browser (`browser.vault`): one file per workspace, `~/.local/share/everythingllm/browser/vault/<workspace>.vault` (0600), sealed with AES-GCM under a key kept apart from the data dir and its backups, `~/.config/everythingllm/browser-vault.key` (made on first use, 0600), with the workspace's name bound in so one workspace's file can't stand in for another's.

- **Using one.** `browser-login` lists the logins (id, site, username, whether it has 2FA and whether it asks first; never a password or 2FA secret) and which fit this chat's page. The agent names a login and the fields from its last read; the runner sends the secret to the driver, which types it in. It never comes back in a reply, a log or the card, and the agent never types a password itself. Once it's in, the agent can't read it back by the page's "show password" button and an edit: a read hides any six characters of a filled secret wherever they show (`driver.hide`), and a field holding one can only be submitted, left or replaced, never typed into, trimmed or selected. Passwords stay hidden for the container's life (only 2FA codes, which go stale, are let go after 20). As a read hides what the agent sends too, a guess sent and seen hidden would spell a secret out, so an address or text the agent sends (or a run of its key presses) that holds a piece of one is refused, and the browser is locked to it until you take it over in the view yourself.
- **Only on its own site.** A login is saved for a site (`linkedin.com`: the host, without `www.`) and fills only there or on a subdomain (`browser.origin`), checked by the runner against the tab and again by the driver against the frame the field is really in, and a password goes only into a password field. So a page that talks the agent into it can't have your LinkedIn password typed into another site. A site is never a public suffix (`github.io`, `co.uk`, from the Public Suffix List kept in `browser/public_suffix_list.dat`), whose subdomains belong to anyone, and a login never fills across one below its site (one for `windows.net` not on `anyone.blob.core.windows.net`), nor is a name that only means something locally (`printer.local`, `nas.lan`, a dotted number). It fills only on an https page and frame on the usual port, never in the clear. From the first password filled, every request the browser makes is checked for the passwords it holds (as typed, URL-encoded or in JSON, `driver.leak`), and one carrying a password anywhere but its own site over https is blocked and said in the next read: a form whose action points elsewhere, or a script, can't send it on.
- **2FA.** A login can carry a TOTP secret (the text under the QR code, or its `otpauth://` address); `browser-login` with `code` fills the current code. That puts both factors in one vault on this machine; leave the secret out for accounts where that's too much.
- **Asking first.** A login marked "ask me before each use" makes the agent wait for your OK: the card says so, and the take-over view lists each waiting request, "The agent in the chat browsing \<its tab's page> wants to use your login for ..., on \<the page's address>", with Allow and Don't allow. Each chat has at most one waiting: a chat that asks for another login replaces its own request (its wait is told so and asks again), never another chat's, so two chats can wait at once. A workspace's main chat and its scheduled jobs are one chat here (thread `default`). A request nobody answers is let go after 30 minutes (`APPROVAL_SECONDS`). An OK lasts 10 minutes for that login in that chat alone (`GRANT`), long enough for the password and the code; another chat asks again. The skill waits up to 5 minutes, then has the agent ask.
- **Adding one.** Never through the chat, where the model would see it. The take-over view has a Saved logins panel to add, list, mark and delete them; it can save and delete, never show a password. And while you have the browser (you took over, or the agent handed it to you), a form you send with a password in it is offered for saving there ("Save the login you just used on ...?"), with the site taken from the frame it came from, whatever the page says (`capture.js`). Offers last 10 minutes and are only ever made while you have it.
- **Asking for one.** When there's no login for the site, `browser-login` with `ask` gives the agent a card for its reply, "Log in to `<who the site belongs to>`" (`google.com` for `accounts.google.com`, `evil.app` for `accounts.google.com.verify.evil.app`, the name above the public suffix, `origin.registrable`, so a long host can't push the real owner out of sight) (`https://<PUBLIC_HOST>:8445/_live/browser/login/<id>.png`, drawn like a progress card and pushed again as it's answered). It links to a page of its own in the take-over view, `/login/<id>/` on :8454: a form with nothing else on it (no noVNC, so it works on a phone) for a username, password and optional 2FA secret, which go into the vault. The runner names the site from the chat's page (`Runner.op_ask_login`), never the model, and the form lets you widen it only to a parent short of a public suffix (`accounts.google.com` or `google.com`); it shows the page's address too, so a page that talks the agent into asking can only ask for its own site's login, in plain sight, and warns when no login in the workspace is for that owner yet. The request's id (`lr-` and 32 hex digits) is the page's only key, so it needs no token and outlives the browser; it waits 30 minutes (`ASK_SECONDS`), takes one answer, and the take-over view lists the waiting ones. You tell the agent in the chat once it's saved, and it logs in with it as with any other.
- **Passkeys.** The vault keeps passkeys too, beside the logins (each entry has a `kind`; the vault keeps only what the runner has a way to use without the agent reading it, so it isn't a store for API keys). Only you make one: take over, press "Make a passkey" in the Saved logins panel, then add a passkey on the site's page. Every page then has a virtual authenticator (Chromium's WebAuthn over CDP, which no page can reach) until one is made, 5 minutes pass or you hand back. What a site makes is saved, asking first since nobody touches a key when it signs in, as the panel next refreshes (or, with it closed, on the hand-back or as the browser stops), as a passkey the site has and the vault doesn't is one nobody can use. The agent signs in with `browser-login` `passkey`, naming the passkey and the page's button for it: the runner and the driver check the page is on the passkey's site over https, as for a login, the driver puts an authenticator holding only that passkey in the page for the click and at most 15 s after (`PASSKEY_SECONDS`), and Chromium itself checks the page may use it. It covers the chat's page, not a popup or a frame of another origin. A passkey can't come from your phone or password manager (they don't give theirs out), it's this machine's alone, so keep another way into the account, and a site that demands an attested authenticator (some banks, work accounts) refuses it.
- Chromium's own password saving is off in every profile, so what you type stays out of the profile. `browser-reset` wipes a profile but leaves the workspace's saved logins; delete those in the panel.

**Where things are.** A workspace's browser is a container, `everythingllm-browser-<workspace>` (image `localhost/everythingllm-browser`, `host/containers/browser/`). It's started on the workspace's first call and stopped after 20 minutes unused and unwatched; the profile outlives it:

```text
browser/profiles/<workspace>/                     cookies, logins, history (mounted at /profile)
browser/downloads/<workspace>/<thread>/           downloads as they're saved (/downloads)
sandbox/workspaces/<workspace>/project/downloads/ where they end up (run-code's /project/downloads)
browser/sockets/<slot>/                           driver.sock and vnc.sock (/run/browser)
```

The profile is in browser-runner's own folder, which no sandbox run mounts, so no run can read the cookies. The container never mounts a folder a run can write, since a run could make it a symlink and podman would mount wherever it points. Downloads are saved to browser-runner's own folder (at most 10 between two reads, 256 MB each), and the runner copies each one to `/project/downloads` after the thread's next call, opening every step without following a symlink (`hostrpc.safefs`). They go to `/project`, not `/shared`, which every other workspace can read. `uv run hostctl browser-reset <workspace>` stops the workspace's browser and wipes its profile.

**The container.** It's hardened like a service container: read-only root (`/tmp` and Xvfb's key maps on tmpfs), every capability dropped, `no-new-privileges`, `keep-id`, 2 GB of memory, 1 CPU, 1024 processes, `--init`, and the repo mounted read-only for the driver's code. Its entrypoint starts Xvfb, x11vnc on `/run/browser/vnc.sock`, and `browser.driver`, which launches Chromium through Playwright with the persistent profile and answers browser-runner on `/run/browser/driver.sock` (hostrpc). A stop tells the driver first, which closes Chromium while its screen is still there, so the profile is saved as closed cleanly; a browser that ends badly anyway doesn't offer to restore its pages (`--hide-crash-restore-bubble`). One tab per chat thread (an API chat's too, by `thread-scope.js`; see "Scopes" under "Code sandbox"); a popup (a login window) becomes the thread's tab until it closes; downloads are saved and alerts answered on their own (confirms are dismissed), and both are reported in the next read. Closing the window ends the container, and browser-runner starts it again on the next call. Chromium's own sandbox is off (it needs user namespaces the container doesn't give), so the container is the boundary.

**Network.** The container is on egress-net with `--dns none`, at one of the four addresses of the `browser` profile in `egress.toml` (`10.89.79.32`--`.35`: the slot it holds while it runs, so at most four browsers run at once; when a fifth is needed, the one unused longest is stopped, unless it's watched or yours). Chromium sends everything, loopback included, through the egress proxy's public port (`:3129`): public hosts on 80 and 443, never CGNAT, the LAN or this machine, and not even PyPI. A renderer taken over by a page could reach other containers on egress-net directly, as any service container could; their servers answer only loopback and their own address (`hostrpc.local_peer`). The proxy loads `egress.toml` when it starts, so a new profile or address needs `uv run hostctl egress-setup` (it asks while a deep-research run is going).

**Mind what it's logged into.** Pages the agent opens can try to instruct it (prompt injection), and the agent has your other tools too. Log the browser only into accounts you'd let it use unsupervised; never email, banking or a password manager. The agent is told to hand over for logins rather than type passwords, and to ask before anything it can't take back. A sign-in with Google, GitHub, Microsoft or Apple stays yours: the handoff names the provider ("Sign in to Google, then hand the browser back") when the chat's page is its sign-in page, and its session stays in the workspace's profile until `uv run hostctl browser-reset <workspace>`. So one Google session opens every "Sign in with Google" in that workspace to the agent, without asking you again.

`uv run hostctl browser-setup` builds the image (`browser-images`), maps :8445/\_live/browser and :8454, and starts `browser-runner` (`host/systemd/browser-runner.service`). Restarting the runner stops every browser (the profiles stay).

## Deep research

`deep-research` does what Claude's research mode does, within one agent tool call (the agent itself is capped at 40 tool calls per reply, `AGENT_MAX_TOOL_CALLS` in `.env`, so the work happens outside the agent). The skill (`anythingllm/agent-skills/deep-research/`) is a thin front: it hands the question, its setup args and the workspace to `research-runner` (`packages/research`), which runs in a service container of its own (`host/quadlet/research-runner.container.in`, see "Its container" below and "Service containers"), and answers at once with the run's live progress card, so the chat is free while the run goes. They talk over a Unix socket the container sees, `storage/everythingllm/research/runner.sock` (see "Services on the host"): `start` returns a run id and its card, `wait(run_id, since)` long-polls up to 45 s for new progress lines and the result, `runs` lists what the runner holds.

**The live card.** `start`'s `card` is a Markdown image in a link, `[![Deep research: <question>](…/_live/research/<id>.png)](…/_live/research/<id>)`, which the agent pastes as it does a link card. research-runner serves both on 127.0.0.1:8450 (`RESEARCH_LIVE_PORT`, `research.live`; its container publishes the port there), which the machine routes `https://<PUBLIC_HOST>:8445/_live/research/` to. The image is `multipart/x-mixed-replace` (server push, `chatimage.live`): the browser keeps showing the newest frame of the connection, so the card's bar, its minutes and its latest progress line move with the run, with no script and with "Render HTML in chat" off. A frame goes out at most once a second, when the run moves on, and again 0.2 s later if no newer one has (`SETTLE`), since Chrome shows a part only once the next part's headers are in; the response ends with the run (green when it saved its report, red when it failed) or after 30 minutes, and a reload asks again. The bar is how far along the run is (`meter` in `job.run` and the pipeline): planning, then the searches against the depth's budget for most of it, then writing, fact-checking and saving. Someone watching the card counts as following the run, as the skill's wait used to. The link opens a page of the run's latest progress lines that reloads itself; there's no page of the report (it's in the chat and the workspace's documents). Each run's line in the run log keeps its `run_id` and `card`, so a run the runner no longer holds (an hour after it ended, or after a restart) gets one frame of how it ended from the log, and an old chat's card still opens a run from before the documents' report, which the research site kept.

A run, step by step:

1. **Plan** -- the planner model splits the question into sub-questions with search queries. The calling agent can make the split itself instead: the skill's `sub_questions` (each a goal, or `{goal, queries}`, at most the depth's workers) and an optional `title` skip this step, and the run log's `stats.plan` says `caller`.
1. **Research** -- one worker per sub-question, all in parallel. Each searches SearXNG (`SEARXNG_URL`: `https://<PUBLIC_HOST>:8888/search` from the container, `http://127.0.0.1:8888/search` on the host), reads pages with publicweb's page reader (`publicweb.pages`: browser-like headers, trafilatura, public hosts only, redirects included, HTML only, so PDFs are skipped) and extracts findings as claim + verbatim quote. Findings whose quote isn't actually on the page are dropped.
1. **Gap check** -- the planner reviews all findings and sends out follow-up workers for gaps and contradictions. If the check fails, the run goes on to writing.
1. **Write** -- the planner writes a Markdown report from the findings only, citing [n]. If that fails, the findings, grouped by sub-question and cited, become the report (`stats.write` in the run log says so).
1. **Fact-check** -- the planner flags sentences the cited notes don't support; the edits are applied in code, citations are renumbered and the source list appended.
1. **Save** -- the report is saved as `storage/anythingllm-fs/research/<slug>.md` (the title's slug, `-2` and on when it's taken), where the agent's filesystem tools can read it, and the live card turns green. A report that can't be saved fails the run. The run log names the file and keeps the key findings, and the result's `reply` has the whole report in a `<report>` tag, for a caller that waits (the gateway).
1. **Into the workspace's documents** -- research-runner can't reach AnythingLLM, so agents-runner, which follows every run from a workspace's chat (see "Telling the chat" below), adds the file to AnythingLLM's documents and embeds it in that workspace (`/api/v1/document/raw-text` with `addToWorkspaces`), titled as the report. The workspace's chats find it there as they find any document. A scheduled job's run has no workspace, and a gateway client's no AnythingLLM workspace, so theirs stay files.

Depth (`quick` / `standard` / `thorough`, default standard) sets workers, steps per worker, gap rounds and a search budget (15 / 40 / 80); see `packages/research/src/research/config.py`. Models are setup args: `PLANNER_MODEL` (`glm-5.3`) and `WORKER_MODEL` (`deepseek-flash`, run with thinking off). A `glm-*` model goes to Z.AI's coding endpoint (a GLM Coding Plan key gets "1113 Insufficient balance" anywhere else), with the Generic OpenAI provider's key when its base path is Z.AI's, else `ZAI_API_KEY`; any other model goes to DeepSeek. The runner reads those keys from AnythingLLM's `.env` for each run (`llm.provider`), and only those. The planner makes a handful of large calls and the workers make the many small ones (263 calls in a thorough run), so the plan's usage limits go to the planning and writing, and DeepSeek's per-token pricing to the bulk. The worker must be a DeepSeek model: `glm-5.3` can't turn thinking off. When Z.AI says the plan's usage is spent (429, or 1113 "Insufficient balance"), the planner switches to `PLANNER_FALLBACK_MODEL` (`deepseek-flash`, with thinking on; `off` turns this off) for the rest of the run; the chat's progress says so, and the run log's `stats.fallbacks` records it. The planner was `deepseek-v4-pro` until 2026-10-04, when that was V4.1-Flash underneath (see below).

DeepSeek notes, found while building it:

- Don't use JSON mode (`response_format`): with it, flash often replies with the wrong keys or just `{"type": "json_object"}`. Plain prompts plus a parse/repair retry are reliable.
- Thinking tokens count against `max_tokens`, so planner calls get large budgets (16k for JSON, 64k for writing and fact-checking; the API allows up to 393,216).
- Since 2026-09-14 DeepSeek serves `deepseek-v4-pro` requests with V4.1-Flash, until V4.1-Pro launches.

Measured runs (2026-10-03):

| depth | time | searches | pages read | findings kept / dropped | sources cited | LLM calls | tokens in / out |
| --- | --- | --- | --- | --- | --- | --- | --- |
| quick | 5 min | 3 | 11 | 59 / 6 | 8 | 29 | 58k / 41k |
| thorough | 11 min | 108 | 73 | 268 / 31 | 25 | 263 | 473k / 86k |
| standard (paced, after the search budget) | 9 min | 18 | 46 | 219 / 12 | 25 | 109 | 267k / 75k |

Search is the limit, not DeepSeek. The thorough run's 108 searches got Google CSE (where most results come from) suspended for "too many requests" along with Brave and DuckDuckGo, and SearXNG returned nothing until they recovered. Since then:

- searches go out one at a time, at least 2 s apart (page reads and model calls stay parallel), and each run has a search budget by depth; once it's used up, workers read from the results they have and further gap checks are skipped;
- when SearXNG returns nothing because engines refused it, the run fails with "Web search isn't working" and names the engines; workers stop after two failed searches in a row so they don't prolong the block. More engines in SearXNG's settings (Ansible) keep search working when one blocks us.

A run doesn't stop when its chat closes, or when AnythingLLM restarts: it belongs to the runner, which saves the report as usual. A run nobody was watching (its card, or a `wait`) when it finished gets `chat_closed: true` in the run log. To find the report, ask the agent (it's in the workspace's documents), or look in `anythingllm-fs/research/`. A run can't be cancelled from the chat: `FORCE=1 uv run hostctl research-setup` restarts the runner, which kills every run in it. Runs are bounded by their search budget either way. At most 2 run at once; another waits its turn, and its progress says so.

**Telling the Nilson app.** The skill sends the chat it was called from (`_lib/scope.js`'s workspace and thread id) with `start`. When a run from a workspace's chat ends, and `NTFY_URL` is set in `~/.config/everythingllm/relay.env`, research-runner posts to the relay's ntfy topic (`research.notify`): "Research ready" or "Research failed", the question's first 120 characters, `run=dr-…,workspace=…,thread=…` as its tags; never the report. The thread is AnythingLLM's numeric thread id (`/api/v1` gives clients slugs only), so the app finds the chat by the run id in the card it drew. A gateway client's run and a scheduled job's (`_jobs`) tell no one.

**Telling the chat.** research-runner can't reach AnythingLLM, so for a run from any of a workspace's chats (the UI's, the API's, Telegram's) the skill also asks agents-runner to `follow` it, with the workspace, and with the chat when it's one in AnythingLLM's UI (see "Telling the chat" under "Delegation"). agents-runner keeps the runs it follows in `~/.local/share/everythingllm/agents/followed.json` and looks for each in research's run log every 30 s; a run's line there (`ok`, `failed`, or `interrupted` once research-runner starts again) is its end. Then it puts the report in the workspace's documents (above) and tells a UI chat, quoting the key findings and saying where the report is. It reads the report only as a plain file directly in `anythingllm-fs/research/`, never through a symlink, and at most 2 MB of it, since research-runner's container, which reads the web, writes both that folder and the run log that names the file. One not there after two days is let go.

Only a restart of `research-runner` kills a run without a result, so while a run is going it has a marker in `~/.local/share/everythingllm/research/runs/running/<id>.json` (its question and when it started), touched every minute. When the runner starts, it moves every marker into the log as status `interrupted`, since none of them can be its own; until then, a marker quiet for its `stale_ms` (3 minutes) belongs to a run that's gone too (`hostctl.run_guard` reads it so), and a fresh one to a run that's going. `uv run hostctl research-setup` and `uv run hostctl units` (when the unit changed) list the live runs and ask before restarting the runner; with no terminal to ask they stop, unless `FORCE=1` (`hostctl.run_guard`). `uv run hostctl restart` and `uv run hostctl deploy` restart AnythingLLM only, so they don't need to ask. The runner runs the code it started with: after changing `packages/research`, `uv run hostctl research-setup` puts it live.

Every run appends one line to `~/.local/share/everythingllm/research/runs/YYYY-MM.jsonl`: the question, how it ended (`ok` / `failed`, with the error; `interrupted` for one killed by a restart; older runs may say `stopped`), whether the chat closed before it finished (`chat_closed`), the report's file, title and key findings (older runs: the research site's URL), its stats (including `tokens`, with `cached` the input the provider served from its prefix cache, `fact_check` and per-worker `workers_detail` with why each stopped: `done`, `budget`, `wasted`, `search-down`, `notes-full`) and every progress line. AnythingLLM keeps only a chat's final reply, so this is the run's record.

**Its container.** research-runner runs in `localhost/everythingllm-service`, hardened as every service container is (see "Service containers"), with 2 GB, 2 CPUs and 256 PIDs (two runs of a few dozen threads each) and `Nice=10`, which podman passes on to it. Its venv is `venvs/research-runner-ctr/`; the first start syncs it from PyPI through the egress proxy, so the socket and the live cards come up a few minutes later that once. It sees, each at its host path:

- the repo, read-only: the code and `host.env`
- in the data dir: `research/` (the run log)
- in storage: its socket folder, and `anythingllm-fs/research/`, where the reports go
- its share of AnythingLLM's `.env` (`~/.config/everythingllm/ctr/research-runner.env`), read-only: the DeepSeek and Z.AI keys and DeepSeek's model, never AnythingLLM's password

It gets the relay's `NTFY_URL` and `NTFY_TOKEN` as values (`EnvironmentFile=`), not the file.

It goes out only through the egress proxy, with the `research` profile: any public host (the pages it reads, DeepSeek and Z.AI), SearXNG by `PUBLIC_HOST` (`SEARXNG_URL`) and the ntfy host on :443 (`NTFY_HOST`, for a self-hosted one), never AnythingLLM. A page the proxy refuses (a LAN or CGNAT address) is skipped as any unreadable page is. Its share of `.env` is written when it starts, so a model key changed in AnythingLLM reaches it at its next restart (`uv run hostctl research-setup`, with no run going).

To run one by hand, in this process rather than the runner (it logs and saves as usual):

```sh
set -a && . ./host.env && set +a && \
  uv run --package research research-run "Why is the sky blue?" --depth quick
```

## Delegation

`agents-runner` (`packages/agents`, `host/systemd/agents-runner.service`, its own venv in `~/.local/share/everythingllm/venvs/agents`) runs **delegations**: a set of tasks the caller defines, each done by AnythingLLM's own agent, headless, and an optional `then` task that gets their replies (`docs/.proposals/agents.md`, kept out of git). The main agent starts one with the `delegate` skill, for work that splits into parts that each need their own searching or reading; reports stay with deep research, whose pipeline did the same job for a hundredth of the cost when the two were compared. `agents-run` starts a delegation by hand:

```sh
set -a && . ./host.env && set +a && uv run --package agents agents-run \
  "Compare two heat pumps" --task a:worker:"Find the COP of model A, with sources" \
  --task b:worker:"Find the COP of model B, with sources" \
  --then planner:"Compare them in a short table"
```

`--material name:file` gives a task (or `then`) a file's text as its material, and `--plain name` sends it as a plain chat.

Over its socket, `storage/everythingllm/agents/runner.sock`: `delegate(goal, tasks: [{name, profile, instructions, material?, tools?}], then?, chat?)` answers at once with a run id and a live card; `wait`, `runs` and `cancel` (tasks that haven't started won't; running ones finish, unused). It also serves `follow` (a deep research run, for its chat), `update_prompt`, the scheduled jobs' `scheduled_jobs`, `schedule_job` and `remind_once`, and `memories` (below), which only skills call.

- **Profiles are workspaces.** A task's `profile` is its role, and each role is an AnythingLLM workspace with its model and a system prompt (`agents/profiles.py`, `agents/prompts/`): `agents-planner` (GLM 5.3) plans, reviews and writes up; `agents-worker` (GLM 5 Turbo, which thinks least) searches and reads, with AnythingLLM's own web tools. Both are on the GLM plan, which AnythingLLM doesn't price. agents-runner makes and sets them through the developer API before its first delegation.
- **Each task** gets a thread of its own in its workspace, gone when the task ends, and at most `AGENTS_SLOTS` (3) run at once across all delegations. `then`'s prompt has the replies quoted in `<result>` tags as material, never instructions.
- **Material and plain chats.** A task's `material` (a draft, notes, findings; 200,000 characters a task, 400,000 in all) goes into its prompt quoted in a `<material>` tag, as data. `tools: false` sends the task as a plain chat rather than to the agent, for judgment over what it's given. The result counts `tokens` per model as well as `cost`, since AnythingLLM has no price for generic-openai, the planner's provider.
- **Containment.** Every tool loads in a headless run, so every skill of ours that writes, acts or delegates refuses a call from an `agents-*` workspace (`_lib/delegated.js`, held by a test). AnythingLLM's built-in tools load there too, and our refusal doesn't reach them, so the ones that would let a task act later or plant text are kept from it by settings that the setup checklist checks (the end of `uv run hostctl install`, or `python3 -m hostctl.machine checklist` from `packages/hostctl/src`): create-scheduled-job is off (`schedule-job` makes jobs instead, and agents-runner disables a job that appears during a delegation; see "Scheduled jobs from a chat"), and it and the filesystem write tools aren't among the tools that run without asking. With those, a task can read and report; it can't write, run code, make a job or delegate again. Gmail's tools, if connected, are AnythingLLM's and load there as well.
- **The live card** is served on 127.0.0.1:8451 (`AGENTS_LIVE_PORT`) and routed by the machine from `https://<PUBLIC_HOST>:8445/_live/agents/`. Its page shows the progress, and every task's reply once the delegation is done, escaped and under a CSP that allows nothing but the page's own CSS (`runs.live`).
- **The run log** is `~/.local/share/everythingllm/agents/runs/` (`runs.runlog`, as research's).
- **Telling the chat.** A delegation from a chat in AnythingLLM's UI, and a deep research run from one, put a notice in that chat when they end (`agents.postback`). The skill sends `chat` (`_lib/scope.js`'s `chatOf`: the workspace and the thread's numeric id, or null for the main chat), only from a chat in the UI (`uiInvocation`: an invocation with its own row's `uuid`). An API or Telegram chat on a thread has the thread (`thread-scope.js`) but no row, and isn't told, since the Nilson app shows a run's end on its card; a scheduled job has no chat at all. AnythingLLM has no way to add a message without its model answering, so the notice goes in as a plain chat turn (`mode: chat`, no tools) through the developer API, with the thread's slug from the internal API's thread list. The notice is the user's side of that turn, marked "EverythingLLM notice (from the server, not the user)", with the job's status, its results quoted in a `<result>` tag as data, and its link; the workspace's model passes it on, and the agent's prompt says such a notice is never a request. AnythingLLM's UI doesn't refresh a thread by itself, so it shows when the thread is reloaded or opened. A notice that can't be posted (the thread was deleted) is logged and dropped. Gateway clients' delegations tell no chat.
- **The daily budget.** AnythingLLM's agent sends every page a task has read again with each step, so a task that reads a lot uses a lot of tokens (millions, for one that read eight pages), and a running task can't be stopped. On DeepSeek that was \$0.20-0.60 a task, which is why both roles are on the GLM plan now; there it's the plan's usage limits that a big delegation runs into. For a profile on a priced model, agents-runner refuses a new delegation once those that started in the last 24 hours cost `AGENTS_DAILY_USD` (default 3; 0 turns it off), counted from the run log: AnythingLLM doesn't price GLM, and running delegations count once they end, which is why at most 8 may be running or waiting their turn at once. The worker prompt asks for few page reads either way.
- **The key.** agents-runner calls AnythingLLM with a developer API key of its own, in `~/.config/everythingllm/agents.env` (`ANYTHINGLLM_API_KEY`, mode 600, put there by hand); `uv run hostctl agents-setup` checks it. Like research-runner, it isn't restarted by `uv run hostctl units` while a delegation is going (`hostctl.run_guard`).

### Scheduled jobs from a chat

A job runs its prompt with every tool approved, so only a chat may make one. AnythingLLM's own tool for it, create-scheduled-job, is turned off: a delegated task can reach AnythingLLM's built-in tools, which our refusal doesn't cover (see "Delegation", Containment), and it can't list, delete or make a job that runs once either (its cron is five fields in UTC, and a "one-off" set with it repeats every year). agents-runner does it instead (`agents/jobs.py`) over AnythingLLM's internal API, logged in with its password from storage's `.env` (`ANYTHINGLLM_ENV` names another), for three skills. Each refuses a delegated task and a scheduled job's call, shows what it'd do, and acts only when called again with `apply: true`, after the user agrees.

- **`scheduled-jobs`** (`action: list | delete | disable`, `id`, `apply`) lists every job: its cron (UTC), its next and last run in the user's time zone (`USER_TIMEZONE` in `host.env`, default Europe/Stockholm), the last run's status, and whether it's a one-off, and a missed one. Delete and disable take any job, but never one with a run queued or going: AnythingLLM stops a running run when its job is deleted or changed.
- **`schedule-job`** (`name`, `prompt`, `schedule`, `tools`, `apply`) makes a recurring job on `schedule`, a five-field cron in UTC; the preview says how far the user's time zone is from UTC now, for the agent to show the times in it. It refuses a name in use or starting `[once]`, and a tool as `remind-once` does.
- **`remind-once`** (`name`, `prompt`, `tools`, `at`, `apply`) makes `[once] <name>`, a job whose cron is that minute, day and month in UTC, from `at`, the user's local date-time. It refuses a time that has passed or is under a minute away, one more than 364 days ahead, one that doesn't exist or happens twice when the clocks change, a name in use, and a tool that `/api/scheduled-jobs/available-tools` doesn't list (or that needs setting up). Any tools may be given; the preview shows them, with the prompt and the time in both zones. The job's reply arrives as AnythingLLM's notification.
- **The registry and the poller.** A one-off made here is recorded in `~/.local/share/everythingllm/agents/once.json` (`{id, name, fire_at, state}`), and every 60 s agents-runner looks at those jobs, never one only named `[once] …`. Two minutes after `fire_at`, with no run queued or going, a completed run started at or after `fire_at` gets the job deleted (with its runs: AnythingLLM deletes them with the job). A job that never ran (missed, e.g. AnythingLLM was down) or whose run failed is disabled, since its cron would run it again a year on, then kept (its result stays readable), logged once and listed as such, and the agent offers to delete it. A run started by hand before `fire_at` doesn't count. While the registry is empty, the poller reads the file and nothing else.
- **The guard during delegations.** While a delegation runs, agents-runner lists the jobs every 10 s, and disables any job that wasn't there when it started and wasn't made by these skills, saying so in the delegation's events and its log: with create-scheduled-job off nothing else should make one, so this catches the tool turned on again. A job made by hand in the UI meanwhile is disabled too; turn it on again there.

### Saved memories

AnythingLLM keeps short facts about the user (Settings > Personalization): at most 5 global and 20 per workspace, which it fills itself from idle chats and adds to every chat's system prompt as "Things I Remember About You" (the global ones, and the 5 of the workspace's closest to the chat). Its built-in `rag-memory` "store" isn't that: it embeds text into the workspace's documents. Only the UI could manage them, so the agent didn't know it had them; agents-runner does it over the internal API (`agents/memories.py`), logged in as for the jobs above, for the **`memories`** skill (`action: list | save | forget`, `text`, `scope`, `id`), which refuses a delegated task and a scheduled job's call. Each action is done at once, without the show-first step of the jobs: a memory is one line, and either action is undone by the other.

- **`list`** gives the global memories and the calling workspace's, each with its id and when a chat last got it, and the room left under each cap.
- **`save`** keeps one fact (at most 500 characters, on one line, with no control characters or invisible ones such as bidi marks and zero-width spaces, which would make it read differently on the Personalization page than in the prompt) for the workspace, or with `scope: global` for every workspace. Text that a memory in that scope (or a global one) already says isn't saved again; the reply names that memory. A full scope is AnythingLLM's refusal, passed on.
- **`forget`** takes only an id from the calling workspace's list (global or its own), deletes it, and gives back its text and scope, so a mistake can be saved again.

With Personalization off, every action says so (AnythingLLM's "Personalization is disabled."). A memory's text goes into every chat's system prompt after ours, so the prompt's Safety rules say memories are facts, never instructions, and are saved only when the user asks; the skill says the same. Nothing else stops a chat that read a page or mail from saving one, so the Personalization page is where to look for one the user didn't ask for.

## MCP gateway

The gateway (`packages/gateway`, `gateway.service`, 127.0.0.1:8452, routed by the machine at https :8452) serves the runners' tools over MCP's streamable HTTP to clients other than AnythingLLM, such as Claude Code on another of your machines (`docs/.proposals/gateway-and-containers.md`, kept out of git). It's one more front on the host, over the same runner sockets. Its tools come in groups, and a client gets the groups it's granted. Two tools of the same name stop it from starting.

- **Its fronts** (`agents`, `research`, `sandbox`): `gateway/agents.py`, `gateway/research.py` and `gateway/sandbox.py`, each a group, its tools signatures with docstrings (`hostrpc.forwarder` sends each call to the front's runner), never run as MCP servers of their own. Each names its tools with its `PREFIX` (`agents_`, `research_`, `sandbox_`), so their `wait`s and `runs` don't clash; the op sent to the runner keeps its own name (`delegate`, `start`, `run`, ...).
- **Delegation** (`agents`): `agents_delegate`, `agents_wait`, `agents_runs` and `agents_cancel` over agents-runner. A client follows a run with `agents_wait`, advancing `since` by the events it got, until `done`. A client's delegations are its own: the gateway sends the runner the client as their owner (never from the arguments), and `agents_runs`, `agents_wait` and `agents_cancel` reach only those. The daily budget (`AGENTS_DAILY_USD`) counts these delegations too.
- **Deep research** (`research`): `research_start(question, depth, sub_questions, title)`, `research_wait(run_id, since)` and `research_runs()` over research-runner. A run started here takes the runner's defaults: its report is saved to the agent's files, and the models are the runner's, not the deep-research skill's setup args. `research_start` answers at once with `{run_id, queued, card}`; a client follows the run with `research_wait` as with `agents_wait`, and once it's done the result's `reply` holds the whole report. A client's runs are its own, as its delegations are: `research_wait` and `research_runs` reach only the runs it started, and only it gets their reports.
- **The code sandbox** (`sandbox`): `sandbox_run(language, code, timeout)`, `sandbox_wait(run_id)`, `sandbox_write(path, content, delete)`, `sandbox_publish(slug, path, remove)` and `sandbox_build_site(path, slug)` over sandbox-runner, the ops behind `run-code`, `write-file`, `publish` and `build-site`. Each call carries the scope `{workspace: "client-<name>", thread: "gateway", gateway: true}`, made from the calling client's name (`gateway.grants.client`), never from the model's arguments: a `scope` argument is dropped, and the gateway's scope is the one sent. So a client has a sandbox workspace of its own, `client-<name>`, with one thread: its `/work` is `workspaces/client-<name>/threads/gateway`, and its pages are `https://<PUBLIC_HOST>:8447/client-<name>/`. A run or a build answers within the runner's 45 s wait; one still going comes back as `{run_id, running: true, seconds}`, and the client calls `sandbox_wait` until it's done. A second 45 s wait wouldn't fit in the call's 55 s (hostrpc's call timeout), so no call runs past what an MCP client's own 60 s limit allows.
- **Sockets.** `hostrpc.caller` asks the socket in a front's `<FRONT>_SOCKET` (its `ENV`), read at each call, so at start the gateway sets each to the host's (`hostenv.socket_path`), unless it's set already.

What the scopes don't do, by design (one user, so documented rather than enforced):

- A `client-<name>` sandbox workspace is a workspace like any other: its runs read every AnythingLLM workspace's `/shared/<workspace>` (read-only), and every workspace's runs read its `/shared/client-<name>`. Its `/project`, `/work` and `/public` are its own, and count toward its own size limit. The runner keeps `client-` workspaces for the gateway's scopes, so an AnythingLLM workspace slugged `client-<name>` is refused the sandbox (and told to rename) instead of sharing that client's folders.

**Clients and tokens.** Every path but `/health` needs `Authorization: Bearer <token>`. Each client has its own token, a `GATEWAY_TOKEN_<NAME>` line in `~/.config/everythingllm/gateway.env` (mode 600), and its name is `<name>` in lowercase, `_` as `-`. A name is at most 63 letters, digits and hyphens, not starting or ending with a hyphen, since it names the client's sandbox workspace too; the gateway won't start with a token whose name isn't one. To revoke a client, delete its line and restart the gateway. The server is stateless HTTP; DNS-rebinding protection allows only `127.0.0.1`, `localhost` and `PUBLIC_HOST` as the Host.

**Grants.** `packages/gateway/src/gateway/grants.toml` (in the repo, beside the code, with no tokens) gives each client its groups, `[clients.<name>] tools = ["agents", "sandbox", …]`. `claude-code` gets every group. A client with a token but no grant gets no tools (the log says so at start), and a key or group the file doesn't know stops the gateway from starting. The gateway reads it at start, so restart it after a change. One MCP middleware, `gateway.grants.Grants`, holds each client to its grant:

- it drops from `tools/list` the tools the client isn't granted;
- it refuses a `tools/call` outside the grant with an error naming the client (JSON-RPC `-32602`, as for an unknown tool);
- it logs each call with the client's name and the tool's, never its arguments or the token (a refusal as a warning), and tests hold that;
- it sets the ContextVar `gateway.grants.client` to the client's name around the call, so a tool can tell who is calling (the sandbox tools make the client's scope from it).

Why it may act where an MCP tool in AnythingLLM couldn't: our tools there are skills, because an MCP call doesn't say which workspace made it, so it couldn't refuse a delegated task. A gateway call is named by its token, and the client's grant says what it may do.

**Setting it up.** `uv run hostctl gateway-setup` makes `gateway.env` with a token for `claude-code` when it's missing, maps the port, and starts the unit. It isn't part of `uv run hostctl install`. It never prints a token.

**Adding a client.** `uv run hostctl gateway-client <name>` (`hostctl.gateway_env`) adds a `GATEWAY_TOKEN_<NAME>` line with a fresh token when `gateway.env` has none for that client (it never replaces one, and makes the file, mode 600, if there isn't one), then prints the command to run on the client's machine, with `PUBLIC_HOST` from `host.env`:

```sh
claude mcp add --transport http everythingllm https://<PUBLIC_HOST>:8452/mcp \
  --header 'Authorization: Bearer <the client's token>'
```

That's the one place a token is printed, so it's run on purpose and its output kept out of anything shared. Run again, it prints the same command with the token the client already has. It also says what `grants.toml` grants the client: a new client needs a `[clients.<name>]` entry there before it gets any tools. Then restart the gateway, which reads the tokens and grants only when it starts (`systemctl --user restart gateway`). `uv run hostctl gateway-client claude-code` prints Claude Code's command after `gateway-setup`.

`uv run hostctl gateway-logs` follows the gateway. A code change to its fronts or to `grants.toml` reaches it when it restarts; `uv run hostctl deploy` doesn't restart it. A client already connected sees new or renamed tools once it reconnects.

## Nilson relay

Nilson is a Flutter chat client (Linux desktop, Android) that talks to AnythingLLM's developer API. Asked with `stream-chat`, AnythingLLM stops the answer when the client disconnects and saves it to the thread only when the stream completes, so an answer whose app closes, sleeps or loses its network is lost. On 2026-10-06, with AnythingLLM 1.16.2, an answer cut off after 15 chunks was missing from the thread three minutes later. An agent's answer (Agent mode, or `@agent`) was the exception: AnythingLLM (1.16.2 and 1.17.0) kept the agent working, calling its tools and saving the whole answer after the client left, so a Stop stopped only the reading. `anythingllm/agent-stop.js`, preloaded beside `thread-scope.js`, aborts the agent's session when the response closes before it ends (`AIbitat.abort`, as the UI's Stop does), so a stopped agent answer goes no further and isn't saved, like any other; a skill call already going finishes. `uv run hostctl health` says when the patch stops fitting or isn't needed any more.

The relay (`packages/relay`, the `relay` service container, 127.0.0.1:8446) makes that one call for Nilson and owns the answer. Each run streams from AnythingLLM to the end in its own task, which no follower owns; the relay never closes the upstream connection because a follower left, only when the run ends or is cancelled.

It sits beside AnythingLLM on AnythingLLM's own origin: the machine routes `https://<PUBLIC_HOST>:3001/` to AnythingLLM as before and `/everythingllm/` on the same port to the relay at `127.0.0.1:8446`; the relay answers with or without that prefix, so the route may strip it or not. Everything else on :3001 (the web UI, both APIs, the websockets) is AnythingLLM's, so a native AnythingLLM client notices nothing, and Nilson needs one address and one key for both:

- Every route but `/health` takes the AnythingLLM developer API key the client gives AnythingLLM itself, as `Authorization: Bearer <key>`. The relay holds no key: it asks AnythingLLM's `GET /api/v1/auth`, remembers a key it took for a minute (by its hash), and starts a run's `stream-chat` with the caller's key, which stays in that run's memory and never reaches the database or a log. A missing or refused key gets AnythingLLM's own answer, 403 `{"error": "No valid api key found."}`; an AnythingLLM that can't be reached is a 502.
- AnythingLLM's developer keys are all alike (each has the whole `/api/v1`), so any key sees and can cancel every run, as it could read every thread.
- `GET /everythingllm/health` is how a client tells the relay is there: its JSON names the service and its features. AnythingLLM answers a path it doesn't know with its web app's page and a 200, so look at the body, not the status.

Errors are `{"error": "..."}`. The routes, under `/everythingllm`:

| Route | Does |
| --- | --- |
| `POST /v1/runs` | `{"workspace", "thread", "clientId", "body"}` starts a run: 201 with the run. `body` is what the client would send `stream-chat` (a non-empty `message`, or `"reset": true` to clear the thread; `mode` and `attachments` as AnythingLLM takes them), forwarded as it came: without `mode` the workspace's own mode answers, `automatic` included. The body is held only in memory for the call, so attachments never reach the database, and no size limit is set (a 20 MB attachment goes through). A `clientId` already used answers 200 with that run and starts nothing; a thread with a running run answers 409. |
| `GET /v1/runs?status=running` | runs with that status (`running`, `done`, `failed`, `cancelled`), oldest first; every kept run without `status` |
| `GET /v1/runs/{id}` | the run (`id`, `clientId`, `workspace`, `thread`, `mode` (the body's, or null), `status`, `createdAt`, `finishedAt`); 404 when unknown or expired |
| `GET /v1/runs/{id}/events` | server-sent events: `chunk` with each chunk AnythingLLM sent, as it came and in order (an agent's `agentThought`s, the closing chunk and the `finalizeResponseStream` with its sources included), then one of `done` `{}`, `failed` `{"error"}` (for a non-2xx answer, an `error` or `abort` chunk, which isn't passed on, or a broken connection), `cancelled` `{}`, and the stream closes. Ids count from 1; `Last-Event-ID: n` starts after n. `: ping` every 15 s while live. Any number of followers. |
| `POST /v1/runs/{id}/cancel` | closes the upstream connection and ends the run `cancelled`; a run that has ended is left as it is |
| `GET /health` | `{"ok": true, "service": "everythingllm", "features": ["runs"]}`, no key |

Runs and their events are in SQLite (`~/.local/share/everythingllm/relay/relay.db`, mode 600), written as each event arrives. A restart fails the runs it cut short with "The relay restarted during the answer." and keeps their events; finished runs are deleted after 7 days (`RUN_RETENTION_DAYS`). The schema's version is SQLite's `user_version`; opening an older database deletes its runs. With `NTFY_URL` set, a finished or failed run posts "Answer ready" or "Answer failed" to that ntfy topic, with the question's first 120 characters and `run=…,workspace=…,thread=…` as its tags; never the answer. A reset isn't notified.

Its settings live in `~/.config/everythingllm/relay.env` (mode 600), outside the repo, which the AnythingLLM container mounts: only the optional `NTFY_URL` and `NTFY_TOKEN`, which are secrets (research-runner reads them too; see "Deep research"). `uv run hostctl relay-setup` makes the file, builds the service image, maps `/everythingllm` on :3001 and starts the container; `uv run hostctl relay-logs` follows it (any app's `<app>-logs`). `relay.app`'s docstring lists the rest of the config. A client's key never appears in a response or a log line, and a test holds that.

The relay runs in a service container (`host/quadlet/relay.container.in`, see "Service containers"), with 512 MB and one CPU. It mounts the repo read-only, its venv folder (`venvs/relay-ctr/`) and its database's folder (`relay/`), and nothing else: it has no socket and nothing in storage. Its secrets come in as values podman reads from `relay.env` on the host, not as a file. Its only way out is the egress proxy's `relay` profile: AnythingLLM at `https://<PUBLIC_HOST>:3001` (`ANYTHINGLLM_URL`, since the container can't reach the host's loopback), the ntfy host on :443 (`NTFY_HOST` in `host.env`, if it isn't `ntfy.sh`), and PyPI for its first sync; nothing else, public or not. So the key check and stream-chat go through the machine's :3001 route rather than straight to AnythingLLM. It listens on `0.0.0.0:8446` inside (`RELAY_HOST`), published on the host's `127.0.0.1:8446`, where the machine's route and the health check reach it. Through that port every connection arrives from the container's own address (`10.89.79.10`), so that is the one peer whose `X-Forwarded-For` and `X-Forwarded-Proto` uvicorn believes (`FORWARDED_ALLOW_IPS`), and with loopback the only one it answers (`LocalPeers`): another container on egress-net that reaches the port gets a 403, logged by its own address.

Where it differs from the original spec: the relay adds nothing to the body and doesn't interpret the answer (no `mode` default, no pieces or citations of its own); a connection to AnythingLLM that breaks mid-answer, or ten silent minutes, fails the run ("The connection to AnythingLLM broke during the answer.") rather than completing it with what came; and a `clientId` is remembered as long as its run is kept, so reusing it later returns that old run.

## SearXNG

The agent's web search goes to a private SearXNG, a metasearch engine: each query fans out to Google, Bing, Wikipedia and others and the merged results come back as JSON. Nothing is indexed locally and no API keys are needed.

SearXNG is deployed by Ansible, not from this repo: the Quadlet unit `searxng.container` and its config in `/srv/searxng/settings.yml` (JSON output on, limiter off). It listens on 127.0.0.1:8888, and the machine routes HTTPS :8888 to it, since the AnythingLLM container can't reach the host's loopback.

AnythingLLM uses it as the search provider (Agent Skills > Web Search > SearXNG), with base URL `https://<PUBLIC_HOST>:8888/search`. The same can be set through the local API, logged in with the password (AnythingLLM's internal API needs it; see "AnythingLLM's password" below):

```sh
read -rsp 'AnythingLLM password: ' pw; echo
token=$(curl -s localhost:3001/api/request-token -H 'Content-Type: application/json' \
  -d "$(jq -n --arg p "$pw" '{password: $p}')" | jq -r .token)
curl -X POST localhost:3001/api/system/update-env -H "Authorization: Bearer $token" \
  -H 'Content-Type: application/json' -d '{"AgentSearXNGApiUrl":"https://<PUBLIC_HOST>:8888/search"}'
curl -X POST localhost:3001/api/admin/system-preferences -H "Authorization: Bearer $token" \
  -H 'Content-Type: application/json' -d '{"agent_search_provider":"searxng-engine"}'
```

Some engines block servers now and then (DuckDuckGo answers 403 and is suspended for a few minutes); the response's `unresponsive_engines` lists them. Check with

```sh
curl -s 'http://127.0.0.1:8888/search?q=test&format=json' | jq '.results | length, .unresponsive_engines'
journalctl --user -u searxng -f
```
