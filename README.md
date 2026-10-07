# EverythingLLM

Source of truth for the local AnythingLLM instance (`anythingllm.service`, a Quadlet unit
rendered from `host/quadlet/` into `~/.config/containers/systemd/`, storage in
`ANYTHINGLLM_STORAGE`, on this machine `/srv/anythingllm/storage`).

## Setting up a machine

On a new machine, or to bring this one up to date:

    git clone <repo> && cd everythingllm         # any folder; the units are rendered with its path
    cp host.env.example host.env && $EDITOR host.env
    uv run hostctl install

`uv run hostctl install` first checks the machine and stops with a list of what's missing. It checks
`host.env`, the tools the units run (podman and uv at fixed paths), lingering
and the storage folder, creating the folders the containers mount inside it.
Then it does the following:

1. renders and starts the units (`uv run hostctl units`)
2. waits for AnythingLLM
3. deploys (`uv run hostctl deploy`)
4. points web search at SearXNG
5. runs every setup target: sandbox, research and sites runners
6. runs `uv run hostctl health`, which checks the machine's routes (see "The machine's routes")
7. ends with a checklist of what only AnythingLLM's UI can do. Each item is ticked when
   it's already done: the chat model and embedder, a DeepSeek key, the agent limits in the
   `.env`, create-scheduled-job off and no job or file tool running without asking, the
   machine's routes answering, SearXNG answering, a workspace, the built-in skills to turn off, Gmail.

Every step only changes what's out of date, so running it again is safe.

## Host config

This machine's settings live in `host.env` at the repo root, which git ignores. To set it
up, copy `host.env.example` and fill it in:

- `PUBLIC_HOST`: the name this machine is reached by over HTTPS. The machine routes the
  apps' ports on it (see "The machine's routes"), the service containers reach AnythingLLM
  and SearXNG by it, and every link the setup hands out uses it.
- `ANYTHINGLLM_STORAGE`: AnythingLLM's storage directory on the host.

These read it:

- hostctl, which passes both on to what it runs
- `hostctl.sync`, for `ANYTHINGLLM_STORAGE`
- the host's systemd units, through `EnvironmentFile=@REPO@/host.env` (filled in by `uv run hostctl units`)
- the site builds, whose URLs come from `PUBLIC_HOST` (the sandbox runner passes zola
  `--base-url https://<PUBLIC_HOST>:8445/<site>`), so `zola.toml` doesn't name the host.
  They find the file at the root of the repo the sites are in, and the container sees it at
  `/mcp/host.env`, so builds that get none of our environment, like a script's
  `sites-write`, use it too.

Code running on the host derives its storage paths from `ANYTHINGLLM_STORAGE`. Inside the
container that variable isn't set, and storage is `/app/server/storage`. Tests ignore
`host.env`, so they run the same on any machine.

### The machine's routes

The network is the machine's, not the repo's. Nothing here runs `tailscale serve` or
configures a proxy; the machine provides, by whatever it likes (tailscale serve, Caddy,
nginx, …), an HTTPS route for each app's `serve` entry in `apps.toml`:
`https://<PUBLIC_HOST>:<https><path>` to `http://127.0.0.1:<port>`.
`uv run hostctl routes` lists them and checks each answers, and `uv run hostctl health` and
the install checklist do too; an app `install` leaves out (agents, the relay, the gateway)
counts once one of its units runs. Today they are:

| Port  | Path              | To      | App                                       |
| ----- | ----------------- | ------- | ----------------------------------------- |
| 3001  | `/`               | :3001   | AnythingLLM                               |
| 3001  | `/everythingllm`  | :8446   | the Nilson relay (when set up)            |
| 8445  | `/`               | :8445   | the pages site (Caddy)                    |
| 8445  | `/_live/browser`  | :8453   | browser live cards                        |
| 8445  | `/_live/research` | :8450   | research live cards                       |
| 8445  | `/_live/agents`   | :8451   | delegation live cards (when set up)       |
| 8445  | `/news/write`     | :8448   | the article writer                        |
| 8447  | `/`               | :8447   | the workspace pages (Caddy)               |
| 8452  | `/`               | :8452   | the MCP gateway (when set up)             |
| 8454  | `/`               | :8454   | the browser take-over view                |
| 8888  | `/`               | :8888   | SearXNG                                   |

What the code counts on from them:

- **They connect from the host's 127.0.0.1.** The relay, the article writer, the live cards
  and the take-over view answer only loopback and their container's own address
  (`hostrpc.local_peer`), and believe `X-Forwarded-For` and `X-Forwarded-Proto` only from
  there. A proxy on the host works; one in a container on a bridge network doesn't.
- **A path's prefix may be stripped or not.** Every server under a path takes its routes
  with or without it.
- **Responses aren't buffered.** The live cards are `multipart/x-mixed-replace` streams,
  and the relay streams chat answers; each part has to go on as it comes (nginx:
  `proxy_buffering off`).
- **A valid certificate for `PUBLIC_HOST`.** Every link is https, and the service
  containers check it when they reach AnythingLLM and SearXNG through the egress proxy.
- **`PUBLIC_HOST` resolves to an address of this machine where the routes listen, not
  loopback.** The egress proxy resolves it through podman's network, as the host does, and
  connects there; on 127.0.0.1 it would reach its own container. `routes` fails on that.
- **Only your own devices reach them.** This is the repo's one assumption about who's
  calling: the pages sites and the live cards ask no one, AnythingLLM asks for its password,
  the take-over view for its token and the gateway for its clients'. `routes` warns when
  `PUBLIC_HOST` resolves to a public address. A tailnet, a LAN or a VPN all do.

### Containers

This repo owns the containers the setup runs, as templates in `host/quadlet/`; besides
the service containers (see "Service containers"), these two:

- `anythingllm.container`: AnythingLLM, pinned by digest, because the log filter depends on
  its internals
- `static_agent.container`: a Caddy container that mounts `host/caddy/pages.Caddyfile` from
  the repo, so its CSPs are versioned, and serves two sites:
  - **the pages site** (:8445): the Zola sites and the link cards, from
    `pages/public/`. `default-src 'self'; script-src 'none'`: no scripts, no inline styles,
    and nothing fetched from another host, so CSS can't send anything out either.
    `form-action 'none'; base-uri 'none'` cover what `default-src` doesn't: no form posts
    anywhere, and no `<base>` repoints a page's links. Its front page and the workspace
    pages' old addresses redirect to :8447.
  - **the workspace pages site** (:8447): every sandbox workspace's `/public`, mounted
    read-only from `sandbox/public/` and served as it is (see "Code sandbox"). It's a port,
    and so a browser origin, of its own, so that whatever its pages run can't post to
    `/news/write`. The same policy, but inline CSS is allowed, and so are inline scripts
    and scripts from the site itself (`script-src 'self' 'unsafe-inline'`), in every
    workspace, only ever in a CSP sandbox: `sandbox allow-scripts allow-downloads`. Each
    page gets an opaque origin of its own, so its scripts can't use storage or cookies,
    read the site's other pages and files (not even its own folder's, with `fetch`), load
    module scripts, submit forms, open windows or new tabs, or show `alert()`s; downloads
    and Caddy's directory listing still work. `allow-same-origin` must never be added: it
    would let one workspace's scripts read and rewrite every other's pages.
    `packages/sandbox/tests/test_pages_browser.py` checks all of this in a real Chromium.

  `publish` and the sandbox's replies warn the agent when a page uses something the CSP
  blocks (scripts, stylesheets, fonts or images from other hosts), since the page would
  otherwise just render without it. They also pass on the page's notices: that it has
  scripts, so the agent tells the user what they do and asks before publishing it, and
  which of the sandbox's limits (storage, `fetch`, module scripts, alerts, `target=_blank`
  links, forms) it runs into.

The host's own units in `host/systemd/` are templates too. `uv run hostctl units` renders all of them:

- `host/quadlet/*.container.in` goes to `~/.config/containers/systemd/`
- `host/systemd/*.service` and `*.timer` go to `~/.config/systemd/user/`

It fills in `@REPO@` (the checkout's path) and the `host.env` settings, overwrites what's
installed (git has the templates' history), then reloads systemd and restarts what changed: a container
whose unit changed, or a host unit that's running. A change to comments alone
restarts nothing. A guarded runner with a run going is left running, and a container whose
image of ours or network isn't there yet isn't started: its app's setup makes them (see
"Service containers"). Nor is one whose egress proxy (its `Wants=`) isn't installed yet:
`uv run hostctl units egress` comes first. Enabling a host unit is up to its app's `uv run hostctl <app>-setup` (see "The apps" below).
A host unit it rendered whose template is gone is retired: stopped, disabled and
deleted. That is how a dropped app's units go, and how a host runner gives way to
its container, whose Quadlet unit of the same name the old copy would hide (the container
is started then, unless it's a guarded runner with a run going). While one of an app's
containers can't start yet, every old host unit of that app stays as it is, so the app is
never left with neither. `uv run hostctl diff` lists what it would retire. Units it didn't render are left alone. Given app names, `uv run
hostctl units relay` installs and retires only those apps' units (a unit the registry no
longer has counts as an app's by its name), so services move into their containers one at
a time; the rest wait for a later run.

Run it from the main checkout. It refuses to run in a worktree, since the units run the
repo they were rendered from. Edit the templates, never the installed copies; `uv run hostctl diff`
shows where the two differ.

An Ansible playbook used to install the two containers' units and `/srv/static-agent-config/`.
It must leave them alone now, or its next run undoes `uv run hostctl units`.

### The apps

Every app this repo runs is declared once, in `packages/hostctl/src/hostctl/apps.toml`: its units
and a label for each, its socket, the HTTPS routes it needs from the machine, whether its restarts wait
for a run (the guard), its health checks, the steps its setup runs first, and whether
`uv run hostctl install` sets it up (and if not, why). hostctl reads it through
`hostctl.apps` (standard library only, like the rest of hostctl);
app code never does. `uv run hostctl apps` lists the apps; for each:

- `uv run hostctl <app>-setup` installs its own units (`uv run hostctl units <app>`, so
  another app's runner never moves into its container on the side), runs its `before`
  steps (the sandbox's and the service
  containers' image builds, the agents and gateway key files, the relay's settings file),
  enables and (re)starts its units and (re)starts its containers,
  asking first while a guarded one has a run going (`FORCE=1` doesn't ask), starts its
  timers, and prints the routes it needs from the machine
  (`hostctl.appctl`).
- `uv run hostctl <app>-logs` follows its units and the ones it watches.
- `uv run hostctl routes` lists every app's routes on `PUBLIC_HOST` and checks each answers
  (see "The machine's routes"); it sets nothing up.
- `uv run hostctl health` checks every app's units, health URLs, routes and sockets (`health.sh`,
  which gets them from `python3 -m hostctl.appctl units`, `health`, `routes` and `sockets`, pinging
  each runner).

Adding an app: its code, its unit template in `host/`, and one entry in `apps.toml`.
`packages/hostctl/tests/test_apps.py` says what's missing: a template no app owns, a unit
without a template, two mappings on one port, or a port that isn't the one the code or the
unit uses.

### AnythingLLM's password

AnythingLLM's own API (`/api/...`, which its UI uses; not the developer API's `/api/v1/`) answers
anyone who reaches it until it has a password, and the machine's :3001 route reaches it. That includes
scheduled jobs, which run the agent with every tool approved, `.env` changes and new API keys.
So it gets a password (Settings > Security > Password protection; long and random, from
`[a-zA-Z0-9_-!@$%^&*();]`), which AnythingLLM keeps in plain text as `AUTH_TOKEN` in storage's
`.env`, beside a `JWT_SECRET` it makes.

Our callers of that API log in with it: `hostctl.sync` and `hostctl.machine` through
`units.anythingllm_headers`; a package that needs it uses
`hostrpc.anythingllm_headers`. Each logs in once per process (a login lasts 30 days and is
logged) and once more after a 401; with no password set they send nothing. The relay holds no
key and doesn't log in: it checks each client's developer API key with `/api/v1/auth` and asks
with that. `uv run hostctl health` fails when
`/api/scheduled-jobs` answers without a login.

What a password doesn't close: `/api/request-token` has no rate limit, so the password has to
be long; a developer API key (any client's, Nilson's included) still has full `/api/v1` access, `update-env`
included; the agent's websocket needs only an invocation's id.

Not in this repo, so a new machine needs them first: rootless podman with Quadlet, systemd
lingering for the user, HTTPS routes to the apps (see "The machine's routes"), uv, SearXNG (deployed by Ansible,
see SearXNG below) and Ollama if it's the embedding provider. AnythingLLM's own settings
(providers and keys in its `.env`, workspaces, which built-in skills are off) are set
through its UI.

## Layout

- `anythingllm/agent-skills/<hubId>/` — custom agent skills (`plugin.json` + `handler.js`)
  - `deep-research/` — multi-source web research with GLM and DeepSeek, published to the
    `research` site; hands the work to `research-runner` on the host (see "Deep research")
  - `run-code/`, `write-file/`, `publish/`, `build-site/` — the code sandbox, run by
    `sandbox-runner` on the host (see "Code sandbox")
  - `browse/`, `browser-act/`, `browser-read/`, `browser-handoff/`, `browser-login/` — the
    workspace's browser and its saved logins, run by `browser-runner` on the host (see "Browser")
  - `update-prompt/` — refreshes the calling workspace's EverythingLLM block in its system
    prompt (below), through `agents-runner`'s `update_prompt`; it shows what would change
    first and writes only with `apply`
  - `scheduled-jobs/`, `schedule-job/`, `remind-once/` — list, delete or disable
    AnythingLLM's scheduled jobs, make a recurring one, and set one-off jobs that are
    deleted once they've run, through `agents-runner` (see "Scheduled jobs from a chat");
    each shows first and acts only with `apply`
  - `memories/` — list, save or forget AnythingLLM's saved memories, through `agents-runner`
    (see "Saved memories"); forget shows first and deletes only with `apply`
  - `write-entry/`, `delete-entry/` — the ops of the sites runner that write. They're
    skills, not MCP tools, so they can refuse a delegated task (below); each forwards one op
    to its runner (`forwardSkill` in `_lib/runner.js`). They're generated: each is declared
    in its front's `server.py` like a tool, a signature with a docstring and no body, under
    `@skills.add` (`hostrpc.Skills`), and `uv run hostctl skills` writes its `plugin.json` and
    `handler.js` from that (`hostrpc.skillgen`), so edit the declaration, not those files.
    `uv run hostctl diff` and `uv run hostctl deploy` stop when they're stale. A param the agent leaves out is
    left out of the op's args, so the op's own default applies; a test holds a declaration's
    parameters and defaults to its op's.
  - `_lib/` — what the skills share (no `plugin.json`, so AnythingLLM doesn't load it as
    a skill): `hostrpc.js`, the node side of `packages/hostrpc`; `sandbox.js`; `browser.js`;
    `scope.js`, a call's {workspace, thread} for the two; `runner.js`; and `delegated.js`, the check that makes every skill of ours that writes, acts or
    delegates refuse a call from an `agents-*` workspace, where delegated tasks will run
    (`docs/.proposals/agents.md`). A test holds every skill to it; elsewhere, chats, the
    Nilson relay's API chats and scheduled jobs, nothing changes.
- `anythingllm/mcp_servers.json` — deployed to `storage/plugins/anythingllm_mcp_servers.json`
- `anythingllm/env.example` — keys used in the live `.env` (values stay out of git)
- `anythingllm/system-prompt.md` — the system prompt for chat and the agent: which tool
  to reach for, the tool-call budget, safety rules. A workspace's prompt is AnythingLLM's,
  edited in its UI, and deploy never writes one: ours goes in as a block marked with its
  version (`hostctl.prompt`), which deploy sets as the default for new workspaces. Deploy
  also keeps the System Prompt Variable `{everythingllm_version}` at the repo's version;
  the block asks the model to tell the user once when it's behind, and the `update-prompt`
  skill refreshes it, keeping the workspace's own text around it. `uv run hostctl health`
  lists the workspaces whose block is behind or missing. Scheduled jobs have no workspace,
  so they get AnythingLLM's built-in prompt instead; their own prompts carry what they need.
- `anythingllm/scheduled-jobs/<slug>/` — scheduled jobs (`job.json` with name, cron and
  tools, plus `prompt.md`), deployed through the AnythingLLM API and matched by name
  (`hostctl.jobs`): two live jobs sharing a repo job's name stop deploy, and the
  `scheduled-jobs` skill won't delete or disable a repo job
  - `daily-news-page/` — writes the day's Daily News edition (US, Sweden, World) to the
    `news` site from the feed headlines of the `sites` server's `headlines` tool; cron is UTC inside the container (18:00 UTC = 20:00 Stockholm in summer, 19:00 in
    winter), and the prompt dates the edition by Stockholm time
- `packages/` — MCP servers we write: members of the uv workspace at the repo root
  (`pyproject.toml`, `uv.lock`), one per subdirectory
  - `packages/sites/` — the Zola sites on the pages site (https :8445): list/write/get/delete
    their entries and build them; and `headlines(section)`, the last 30 hours' stories for
    the Daily News job from the feeds in `FEEDS` (`sites/feeds.py`), each with its own link.
    The MCP server forwards to `sites-runner` on the host, which does the work. The sites'
    sources are in `packages/sites/zola/` (see below)
  - `packages/sandbox/` — not an MCP server: `sandbox-runner` runs the agent's Python and bash
    in throwaway podman containers on the host, with only PyPI on the network, and
    publishes pages from them, for the `run-code`, `write-file`, `publish` and `build-site` skills (see
    "Code sandbox" below)
  - `packages/browser/` — not an MCP server: `browser-runner` runs a Chromium per workspace
    in a hardened container, with a live card per chat and a take-over view, for the
    browse skills (see "Browser"); `browser.driver` runs in that container
  - `packages/hostrpc/` — a library, not a server: how the MCP servers and skills talk to the
    services on the host (see "Services on the host" below)
  - `packages/research/` — not an MCP server: `research-runner` runs the deep-research skill's
    runs, in a service container of its own, and `research-run` runs one by hand (see "Deep
    research")
  - `packages/agents/` — not an MCP server: `agents-runner` runs delegations, tasks done by
    AnythingLLM's own agents, and `agents-run` starts one by hand (see "Delegation")
  - `packages/runs/` — a library, not a server: what research-runner and agents-runner share
    for long runs: run state with long-poll waiting and slots, the run log, live cards
  - `packages/publicweb/` — a library, not a server: the HTTP client sites and research use,
    which refuses LAN, CGNAT (Tailscale's) and loopback hosts, and `publicweb.pages`, the page reader on
    it that the article writer and research share
  - `packages/chatimage/` — a library, not a server: the pictures the host draws for the chat,
    which the agent shows as Markdown images: link cards for published pages (see "Code
    sandbox"), deep research's live progress cards, and the server push that keeps a live one
    current (see "Deep research")
- `packages/egress/` — the egress proxy, the service containers' only way out, and
  `egress.toml`, their addresses and what each may reach (see "Service containers")
- `packages/relay/` — the Nilson relay, a service (in its own container) for the Nilson chat
  app rather than for AnythingLLM's agent; also a workspace member (see "Nilson relay")
- `packages/gateway/` — the MCP gateway, a host service that serves the fronts' tools over
  HTTP to MCP clients other than AnythingLLM (see "MCP gateway")
- `host/systemd/` — host user units, rendered into `~/.config/systemd/user/` (`uv run hostctl units`);
  each one's `Description=` says what it does, and its app's `uv run hostctl <app>-setup` (see "The
  apps") enables it.
- What only host services read or write lives in `~/.local/share/everythingllm`
  (`hostrpc.data_dir()`), not in AnythingLLM's storage, which the container mounts. It's
  laid out by kind:

      venvs/<name>/        the host services' venvs (agents, browser, gateway, sandbox)
      venvs/<x>-ctr/       a service container's venv and uv cache (venv/, uv-cache/):
                           egress-proxy, relay, research-runner, sites-runner
      pages/public/        the pages site Caddy serves
      pages/entries/       the Zola entries
      sandbox/workspaces/  the sandbox's folders, one per workspace (threads/, project/,
                           shared/), and beside them the workspace's browser profile
                           (browser/, browser-runner's)
      browser/             browser-runner's: each running browser's sockets (sockets/<slot>/),
                           noVNC for the take-over view (novnc/) and the saved logins
                           (vault/<workspace>.vault, sealed)
      research/runs/       the deep-research run log and live runs' markers
      agents/runs/         the delegations' run log and live runs' markers
      relay/               the Nilson relay's database

  Storage keeps AnythingLLM's own data, the runners' sockets (`storage/everythingllm/<name>/runner.sock`,
  which the container reaches) and what AnythingLLM reads (`anythingllm-fs/research/`,
  `documents/`).
- The `static_agent` Caddy container mounts just `pages/public/` read-only and serves it on
  127.0.0.1:8445
- `host/quadlet/` — the Quadlet units, as templates (`uv run hostctl units`): AnythingLLM,
  the pages site and the service containers
- `host/containers/` — the images we build: the sandbox's (`uv run hostctl sandbox-images`),
  the service containers' (`uv run hostctl service-images`) and the workspaces' browser
  (`uv run hostctl browser-images`)
- `host/caddy/pages.Caddyfile` — the pages site's Caddy config, including its CSP
- `packages/hostctl` — `uv run hostctl <command>`, everything that sets up, syncs and checks
  the host (`cli`, the commands; `uv run` installs it into the dev venv first, so a fresh clone
  needs only uv). Standard library only, so `health.sh` and the apps' `before` steps run its
  modules with any `python3`. `hostctl.skills` is the exception: it imports the fronts, so it
  runs in the whole workspace's venv.
  - `sync` — diff/deploy/import between this repo and live storage
  - `units` — renders and installs `host/quadlet/` and `host/systemd/` (`uv run hostctl units`)
  - `machine` — `uv run hostctl install`'s checks, its wait for AnythingLLM, the web search setting
    and the closing checklist
  - `appctl` — the apps' setup, logs and routes, from the registry
  - `run_guard` — asks before a runner with a live run restarts
  - `agents_env`, `relay_env`, `gateway_env` — the agents, relay and gateway setups' key
    file checks; `gateway_env` also adds a gateway client (`uv run hostctl gateway-client`)
  - `ctr_env` — a service container's share of AnythingLLM's `.env`, which its template's
    `ExecStartPre` writes before each start
  - `skills` — writes the generated skills (`uv run hostctl skills`)
  - `health.sh` — `uv run hostctl health`

## Workflow

`uv run hostctl` lists every command. Day to day: `uv run hostctl diff` shows what would change live,
`uv run hostctl deploy` copies it into storage, refreshes the MCP deps
and restarts AnythingLLM, `uv run hostctl test` runs every test and `uv run hostctl health` checks every unit,
port, host service and runner socket. `uv run hostctl import-skill <hubId>` (and `import-job`)
brings something made in the UI under the repo. Slash commands aren't in the repo:
they're AnythingLLM's, made and changed in its UI.

Skill handlers are re-required on each load, so skill changes don't need a restart, but
`uv run hostctl deploy` also runs `uv run hostctl mcp-sync` and `uv run hostctl restart`, so AnythingLLM and every MCP
server it starts run the code and deps that were just deployed.
On deploy, a skill's `active` flag and any setup_args `value` saved through the UI
are kept from the live `plugin.json` unless the repo sets a `value` itself.
Scheduled jobs keep their live enabled toggle; deploying one reschedules it right away.

## uv cheatsheet

The repo root is a uv workspace; each `packages/<name>/` is a member with its own dependencies
and console scripts, all locked together in `uv.lock`. Run these from the repo root; the dev
venv is `.venv` there, which is the interpreter `.vscode/settings.json` points at.

    uv sync --all-packages                     # install every member + dev deps into .venv
    uv run --all-packages --all-extras pytest -q   # all tests (what `uv run hostctl test` runs)
    uv run --package sites --extra host pytest packages/sites -q   # one member's tests
    # The tests that build real sites run zola in the sandbox image (the only zola there
    # is), through podman; they skip without it (uv run hostctl sandbox-images).

    uv run --package sites sites-mcp           # an MCP server over stdio (waits on stdin)

    uv add --package sites httpx               # add a dependency to one member
    uv add --package sites --dev pytest-cov    # ...or to its dev group
    uv remove --package sites httpx
    uv lock                                    # re-lock after editing a pyproject.toml
    uv lock --upgrade-package mcp              # bump one dependency
    uv tree --package sites                    # what a member pulls in

- `--package <name>` picks the member whose dependencies and scripts to use; the
  workspace root has no project of its own.
- `--frozen` uses `uv.lock` as-is and never re-locks; `mcp_servers.json` and
  the host units use it so a running service never rewrites the lock.
- `--no-dev` leaves out dev groups. Don't combine it with `.venv`: uv syncs the
  venv to match, so it uninstalls pytest. Point `UV_PROJECT_ENVIRONMENT` at another venv
  instead, as the host units do.
- A member whose MCP server is a front for a host service keeps its base dependencies to
  what the front imports, and puts the rest in a `host` extra (`sites`); its
  units run with `--extra host`. AnythingLLM starts each front with `uv run --package`,
  which installs that member's base dependencies, so the runner's (the
  llm client, the card drawing) stay out of the container.
- After `uv.lock` changes, run `uv run hostctl mcp-sync` (or `uv run hostctl deploy`, which runs it) so the
  container's venv catches up. It installs exactly the members `mcp_servers.json` runs
  (`hostctl.sync mcp-packages`), and removes anything else.

## Zola sites

For sites an agent keeps adding to, the agent writes entries, not HTML, and Zola does the
rest. `packages/sites/zola/themes/agent-site/` is the shared theme (layout, entry lists, a year-grouped
archive, Atom feed; no scripts or inline styles, so it passes the CSP). Each
`packages/sites/zola/sites/<name>/` is one site: `zola.toml` (its `base_url` is `…:8445/<name>`), section
`_index.md` files, and any templates or `static/` CSS it overrides or adds. Templates are
Tera 2: reusable pieces are `{% component %}`s (global, no import), not macros.

The `write-entry` skill (through `sites-runner`, as the `sites` MCP server's reading tools
are) writes entries to `~/.local/share/everythingllm/pages/entries/<name>/<section>/<slug>.md`
(JSON front matter, fields under `extra`) and then rebuilds that site itself, so an entry
is live when `write-entry` returns; if the site doesn't build, the write or delete is
undone ("not saved: the site didn't build: …"). Bodies can't use Zola shortcodes or Tera:
`{{`, `{%` and `{#` get a zero-width space between the characters. zola itself runs only in
the sandbox (below): in a container with no network, so a template's `load_data` can't
fetch anything, with nothing of the host's environment or files but the site's, and
stopped after 40 s. No host and no service container has a zola; `sites.build` refuses a
site whose `zola.toml` names no `theme_from`, with an error that says so, and a test holds
every repo site to naming one. The
front matter names the slug, so a file like `2026-10-01-notes.md` keeps its date in the URL. The build (`sites.build`, also the `sites-build`
command) has the sandbox build the site from the repo plus its entries next
to `~/.local/share/everythingllm/pages/public/<name>/` and swaps it in, holding a lock on `.build.lock` in the entries folder. Entries live outside storage because
only host services read or write them; the AnythingLLM container never needs them.
Built sites carry a `.zola-site` marker; the build won't replace a directory without one,
and the sandbox won't publish over a directory that isn't its own page.

**Built in the sandbox.** A site whose repo `zola.toml` names its theme's origin,
`[extra.build] theme_from = "system"` (the repo's `packages/sites/zola/themes`) or a sandbox workspace's
name (its `/shared/<name>/themes/<theme>`), is built by the sandbox: `sites.build`
asks `sandbox-runner` (`build_system_site`), which builds it in a container with no
network: the site's repo source and its entries mounted read-only, the theme put in place
by the repo's `sitebuild.py`, and the output copied (plain files only) into
`pages/public/.<name>.new`, which `sites.build` marks and swaps in as before. News and
research are both built that way, with `theme_from = "system"`, so they look and
build exactly as they did; pointing one at a workspace's theme is a one-line change to its
`zola.toml`, after which that workspace's theme edits restyle the site at its next build.
Their entries stay on the host and are written exactly as below; no host service reads or
writes a sandbox folder. The operation takes only a site's name and reads the rest from
the repo, since its socket is reachable from the AnythingLLM container. `sandbox-runner`
also serves it alone, with `ping`, on a second socket,
`storage/everythingllm/sandbox-build/runner.sock` (`SANDBOX_BUILD_SOCKET`), which is what
`sites.build` asks and what the sites and research containers mount: the runner's own
socket takes whatever scope a caller names, so a container that reads the web must never
have it. The copy into `.<name>.new`, and `sites.build`'s marker, follow no symlink and
write over nothing, since those containers can write `pages/public/` while the host copies
into it. With `sandbox-runner` down, those sites can't build, so their writes fail and are
undone.

The sites MCP server's builds are started on the host, in `sites-runner` (in a service
container of its own; see "Service containers"),
as every other writer's are; nothing in AnythingLLM's container builds. Other writers use the
`sites-write` command (entry as JSON on stdin; it saves, builds and prints the URL, or
exits 1 with `{"error"}` and keeps nothing when the site doesn't build), so the
entry format has one implementation. The Python writers (the article writer, research-runner)
call `SiteStore` directly instead.
Templates, stylesheets, `zola.toml` and sections change only in the repo; the agent has no
tool for them, except through a theme a site takes from a workspace (above). `uv run hostctl deploy` rebuilds every site on the host (`uv run hostctl sites-build`), so
changes go live with it. A test holds every template to `sites.lint` (no `load_data` or
`get_env`, no scripts, forms, frames, `<base>`, `style=` or event handlers, and `| safe`
only on page or section content), on top of the CSP and the network-free build.

`list_sites` shows the agent each site's sections and the `agent_help` text from
`[extra]` in its `zola.toml`, which is where a site documents its fields.

A site adds its own stylesheets from `static/`, listed in `stylesheets` in its `zola.toml`
and loaded after the theme's `agent-site.css`. Zola copies `static/` as is
(`compile_sass = false`).

The sites:

- `news` — the Daily News: the home page (`/news/`) always shows the newest edition,
  each edition stays at `/news/editions/YYYY-MM-DD/`, and `/news/editions/` is the archive.
  Headlines open articles the bot writes on the first click, kept at
  `/news/articles/<desk>-<n>-<day>/` (see "News articles").
- `research` — reports from the deep-research skill at `/research/reports/<title-slug>/`.

A section can set this under `[extra]` in its `content/<section>/_index.md`:
- `agent_readonly = true`: the `sites` server (and `sites-write`) can read the section
  but refuses to write or delete there. The news `articles` are only written by the article writer.

A new site: add `packages/sites/zola/sites/<name>/` with `theme = "agent-site"`,
`[extra.build] theme_from = "system"`, its sections and an `agent_help`, run `uv run hostctl
deploy`, and point a job or chat at the `sites` server.

## MCP servers in AnythingLLM

The repo is mounted read-only into the AnythingLLM container at `/mcp` (see the
`Volume=` line in `host/quadlet/anythingllm.container.in`), and `mcp_servers.json` launches each server
with `uv run --frozen --project /mcp --package <name>`. The container's venv and uv
cache live in `/srv/anythingllm/storage/everythingllm/mcp/`. The container can't reach the host's
loopback, so servers run inside it over stdio rather than as host HTTP services. Other MCP
clients get the same tools over HTTP from the gateway (see "MCP gateway").

### Services on the host

Work that is heavy, long or needs the host goes to a service outside AnythingLLM instead,
with the MCP server or skill in the container as a thin front: `sandbox-runner` (the code
sandbox), `browser-runner` (the workspaces' browsers) and `agents-runner` (delegation)
as host units, and `research-runner` (deep research) and
`sites-runner` (the sites tools and their builds) in service containers of their own (see
"Service containers"). Each listens
on a Unix socket in storage, `storage/everythingllm/<name>/runner.sock` (mode 0660), which the container
sees without a Quadlet change, and they all speak `hostrpc`'s protocol: one request per
connection, a line of JSON each way, `{"op", "args"}` in and `{"ok": true, "result"}` or
`{"ok": false, "error"}` out.

- `hostrpc.Service(ops, errors=…)` dispatches each request to the function of that name in
  `ops` (a package's `tools.OPS`) or to an `op_<name>` method of a subclass (research,
  sandbox), running one that isn't a coroutine in a thread; a `hostrpc.RunnerError` becomes
  the error the caller sees, as does that of the service's own `errors` (sites-runner's
  `SiteError`); anything else is logged and reported as `runner error: …`. Every service answers `ping`, which
  `uv run hostctl health` asks. `hostrpc.serve` serves one on its socket and removes the
  socket on SIGTERM, or when a `stop` event is set; `hostrpc.run` is a runner's `main()`
  around it, and `hostrpc.serving` serves one for the length of a test.
- `hostrpc.request(socket, op, args, timeout, name=…)` asks one, raising `RunnerError`
  (also when nothing listens). An MCP server gets its `call(op, args)` from
  `hostrpc.caller(folder, env, name, error=ToolError)`, which turns that into a tool error,
  and `hostrpc.forwarder(call, mcp.add_tool)` makes each tool from a signature and docstring
  alone: calling it sends every argument as the op of its name. The skills speak the same
  protocol from node (`anythingllm/agent-skills/_lib/hostrpc.js`); an op that writes or acts
  is declared the same way under a front's `hostrpc.Skills` and becomes a generated skill
  (`uv run hostctl skills`, see "Layout").
- AnythingLLM gives up on a tool call after 60 s, so an op answers within 45 s, and work
  that takes longer carries on in the service (a run id to wait on) or in a unit of its own.
- The container maps the host user (`UserNS=keep-id`), so what a service writes in storage
  is the container's to read and the other way round, and file locks work across both.
- A new one: an `OPS` tuple and a `main()` that calls `hostrpc.run` in the package's
  `tools.py` (`packages/sites` is the example), a `<name>-runner` console script, a unit
  `host/systemd/<name>-runner.service` with its own venv in `~/.local/share/everythingllm/`, and
  an app `<name>` in `apps.toml` with `runner` naming that unit (see "The apps"). Its socket
  is `storage/everythingllm/<name>/runner.sock`, where `hostrpc.caller` looks. A runner may
  be a container instead (see "Service containers"): its `runner` is then the container's
  `<x>.service`.

Code edits go live the next time AnythingLLM starts the server (restart it from the
Agent Skills > MCP Servers page, `uv run hostctl restart`, or `uv run hostctl deploy`, which restarts). Note
that this runs whatever is in the working tree, committed or not. Requires `mcp` 2.x (`MCPServer`, not `FastMCP`).

The machine routes HTTPS :8445 to the pages site and :8447 to the workspace pages site
(see "The machine's routes").

### Service containers

A host service can run in a container of its own instead of as a host unit, hardened like
the sandbox's containers and with one way out, the egress proxy. Each service moves over
on its own: its template goes from `host/systemd/<x>.service` to
`host/quadlet/<x>.container.in`, and its app's `runner` and journal key follow (`apps.toml`'s
`container`, `systemd-<x>`). The next `uv run hostctl units` retires the old host unit: it
stops, disables and deletes its installed copy (systemd prefers
`~/.config/systemd/user/<x>.service` to the unit Quadlet generates under the same name),
then starts the container; a guarded runner with a run going is left for a later run.
`uv run hostctl diff` lists what it would retire. So far the relay, research-runner
and sites-runner have moved; the old venvs in `venvs/<name>/` can go once their containers work.

**The image.** Every service container runs `localhost/everythingllm-service`
(`host/containers/service/Containerfile`): `python:3.12-slim`, the host's uv copied from its
own image, tzdata, the DejaVu and Liberation fonts chatimage draws with, and CA
certificates. It holds none of our code. `uv run hostctl service-images` builds it and
creates `egress-net`; the `egress` app's setup runs it first.

**The paths are the host's.** The repo is mounted read-only at its own path (`@REPO@`), and
the container runs `uv run --frozen --no-dev --project @REPO@ --package <pkg> [--extra host]
<script>` with `HOME=%h`. Everything else it mounts (its folders in the data dir and in
storage, its socket folder, the sandbox's build socket) is mounted at its host path too, so a path
means the same inside and out: what the sandbox's `build_system_site` hands back, what a
runner tells the container, what lands in a run log. `host.env` comes in through
`EnvironmentFile=`, as does an app's own secrets file (`relay.env`): podman reads them on
the host and passes the values in, so they aren't mounted. It takes each value as it is,
quotes included, and the template's own `Environment=` lines win over both.
AnythingLLM's `.env` is never mounted: it holds every provider's key, the password and the
signing secrets. A container that needs a key of it gets its share instead, a file with
just those keys that `hostctl.ctr_env` writes on the host before every start (the
template's `ExecStartPre`), in `~/.config/everythingllm/ctr/<x>.env` (mode 600), mounted
read-only and named by `ANYTHINGLLM_ENV`. The template lists the keys; a key whose value
isn't needed, only whether it's set (`JWT_SECRET`, by which `hostrpc.anythingllm_headers`
knows the password is on), goes in as `set`. A key changed in AnythingLLM's settings
reaches a container at its next restart. Each container has one folder of its own,
`~/.local/share/everythingllm/venvs/<x>-ctr/`, with its venv (`UV_PROJECT_ENVIRONMENT=…/venv`)
and its uv cache (`UV_CACHE_DIR=…/uv-cache`) in it: one mount, so uv can hardlink, and no
container can touch another's packages. The first start syncs the venv from PyPI through
the proxy (a minute or three); later ones find it synced.

**A code change reaches a container by a restart**, as it does a host unit: `uv run hostctl
<app>-setup`, or `systemctl --user restart <x>.service`. `<app>-setup` restarts an app's
containers (it doesn't enable them: Quadlet's `[Install]` does), asking first while a
guarded runner has a run going, as for a host unit. `uv run hostctl units` starts a changed
container, except a guarded one with a run going, and one whose image, network or egress
proxy isn't there yet, which waits for its app's setup (or `units egress`). The egress
proxy is guarded by research's runs, since its restart cuts their requests.

**Hardening.** Every service container's template has these Quadlet keys
(`packages/egress/tests/test_quadlet.py` holds them to it):

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

Quadlet in podman 5.4 has no `Memory=` or `Umask=`; `PodmanArgs` carries them, and
`hostctl`'s tests convert every template with `/usr/libexec/podman/quadlet -dryrun`, which
refuses a key it doesn't know. A template never sets `ContainerName=`: Quadlet's
`systemd-<x>` is the name `<app>-logs` finds its journal by. Inside, the
`anythingllm` group shows as `nogroup` (65534): access through it works, but code can't
chgrp to it or look it up by name; storage's setgid folders give new files the group
anyway.

**Ports and addresses.** A service's HTTP port is published on the host's `127.0.0.1`, so
`apps.toml`'s `serve` and health checks are unchanged. What comes through arrives from the
container's own address, not its loopback, so a server in a container listens on `0.0.0.0`:
`LIVE_HOST` (the live cards, `runs.live`), `ARTICLES_HOST` (the article writer) and
`RELAY_HOST` (the relay) say so in its template, and default to `127.0.0.1` on the host.
For the same reason a server that believes the machine's route's `X-Forwarded-For` and
`X-Forwarded-Proto` believes them from its container's own address, not `127.0.0.1`: the
relay's template sets uvicorn's `FORWARDED_ALLOW_IPS` to it. Listening on `0.0.0.0` would
also let every other container on egress-net reach that port (podman's bridge doesn't keep
them apart), and a container that a feed or a page had taken over could have articles
written and sites rebuilt. So each of these servers answers a connection only from
loopback or its own address, the socket's local one (`hostrpc.local_peer`; the relay's
`LocalPeers` runs it outside uvicorn's proxy headers, which would put the forwarded client
in the peer's place), and refuses any other with a 403: it works unchanged as a host unit
on `127.0.0.1` and behind the published port, and another container, coming from an
address of its own, gets nothing. A container can't reach the
host's loopback either, so it reaches AnythingLLM and SearXNG by `PUBLIC_HOST`
through the proxy: `ANYTHINGLLM_URL=https://<PUBLIC_HOST>:3001` (the relay) and
`SEARXNG_URL=https://<PUBLIC_HOST>:8888/search` (research, the article writer). All default
to the host's loopback.

**The egress proxy** (`packages/egress`, the `egress` app) is egress-net's only way out.
`egress-net` is an internal podman network (`10.89.79.0/24`), with no route and no DNS. podman gives a container that names no
address one from `10.89.79.128/25` (`ip_range`), apart from every service's, so a stray one
can't take a stopped service's address and its profile. `egress-proxy` runs in a container of the same
image, on egress-net at `10.89.79.2` and on podman's default network for its own way out,
and listens at `10.89.79.2:3128`, and at `:3129`, its public port (below). It takes `CONNECT host:port` (https) and absolute-form
plain-http requests, and judges each by the caller's address on egress-net and the host and
port asked for:

- `packages/egress/src/egress/egress.toml` gives each container its address (`ips`) and
  each service a profile: `public` (any host whose addresses are all public, on ports 80 and
  443) and `allow`, `host:port` exceptions reached whatever their address. Every profile
  also allows `pypi.org:443` and `files.pythonhosted.org:443`, for uv. `@PUBLIC_HOST@` and
  `@NTFY_HOST@` (default `ntfy.sh`) come from the proxy's environment.

  | profile  | containers (address)                                        | public | allow                         |
  |----------|-------------------------------------------------------------|--------|-------------------------------|
  | relay    | relay (.10)                                                 | no     | `PUBLIC_HOST:3001`, ntfy :443 |
  | research | research-runner (.11)                                       | yes    | `PUBLIC_HOST:8888`, ntfy :443 |
  | sites    | sites-runner (.12)                                          | yes    | `PUBLIC_HOST:8888`            |
  | browser  | the workspaces' browsers (.32–.35, one per slot)            | yes    | none                          |
  | sandbox  | the code sandbox's runs (.40–.41, one per slot)             | no     | none (PyPI, as every profile) |

- A public host must resolve to public addresses only, all of them: the rule is
  `publicweb.public_address`, the one the services use on the host, so loopback, the LAN,
  link-local, the CGNAT range (Tailscale's) and IPv4-mapped forms of them are all refused. The
  proxy resolves each name once and connects to the address it checked, so a name that
  answers differently the second time (DNS rebinding) gets nowhere.
- Anything else is refused with a 403 that says why, as is a connection from an address no
  profile has. Each connection logs its profile, method, `host:port` and verdict, never a
  path or a query (`uv run hostctl egress-logs`).

In a container, `EGRESS_PROXY` puts `publicweb.public_client` in proxy mode: every request
goes to the proxy, which makes the address check, and the client checks only the scheme.
It names the proxy's public port, `:3129` (`public_port` in egress.toml), where only
`public` counts and no `allow` exception does, PyPI's included: `public_client` fetches
URLs that came from the web or the agent, and on the host it refuses CGNAT and the LAN, so a page
or a redirect mustn't reach AnythingLLM or SearXNG through the container's exceptions
either. Other clients (the services' own httpx clients for AnythingLLM, SearXNG, DeepSeek
and ntfy, and uv) follow `HTTPS_PROXY` and `HTTP_PROXY`, on `:3128`. On the host none of
these is set, and nothing changes.

**sites-runner** is one (`host/quadlet/sites-runner.container.in`, the `sites` app, journal
`systemd-sites-runner`), with the article writer in the same process. Besides the repo and
its own venv folder (`venvs/sites-runner-ctr/`), it mounts, each at its host path:

- `pages/entries/` and `pages/public/` in the data dir: it writes entries, takes the lock
  in `pages/entries/.build.lock` that a host `sites-build` takes too, draws link cards and
  swaps built sites in
- `storage/everythingllm/sites/`, its socket's folder (so `GroupAdd=keep-groups`)
- `storage/everythingllm/sandbox-build/`, read-only: the sandbox runner's build socket,
  which serves `build_system_site` and nothing else. The sandbox writes `pages/public/.<site>.new`, which the runner sees
  at the same path and swaps in. Connecting to a socket needs no write access to its
  folder, and a folder rather than the socket itself keeps working when the sandbox runner
  makes a new one
- its share of AnythingLLM's `.env` (`~/.config/everythingllm/ctr/sites-runner.env`),
  read-only: the article writer's DeepSeek key and model, nothing else

Nothing else of storage or the data dir; no podman socket. There's no zola in the image:
the sandbox builds every site (see "Zola sites"). The article writer
listens on `0.0.0.0:8448` (`ARTICLES_HOST`), published on the host's `127.0.0.1:8448`, and
searches `SEARXNG_URL=https://<PUBLIC_HOST>:8888/search`; the feeds, the story pages and
DeepSeek are public hosts, which its profile (`sites`) lets through. A feed or page the
proxy refuses fails as one that didn't load (`httpx.ProxyError`, or a 403), as on the host.
Its limits: 1 GB of memory, one CPU, 256 PIDs. The host unit's venv, `venvs/sites/`, is
unused once it has moved, and can go.

## Code sandbox

Four agent skills give the agent a small Linux machine to run code in, like the Claude
app's, and a way to publish what it makes:

- `run-code` runs a Python or bash script and replies with its output. It waits for the
  whole run (up to 300 s), showing in the chat that it's still going; skills, unlike MCP
  tools, have no 60 s limit. Reading, listing, moving and deleting files is bash.
- `write-file` writes a text file, or deletes a file or folder (deleting exactly one of the
  workspace's folders empties it).
- `publish` gives a page's link and card, lists the workspace's pages, copies a file or
  folder from elsewhere into `/public`, or removes a page.
- `build-site` builds a Zola site from the workspace's folders into `/public/<slug>`, which
  puts it live (see "Building sites").

**Pages are `/public`, served as they are.** A workspace's `/public` is its pages on the
web, at `https://<PUBLIC_HOST>:8447/<workspace>/`: `public/notes/index.html` is
`/<workspace>/notes/`, and any other file is served as it is. Whatever is written there is
live at once, and deleting it takes it down; there's no copy, no sync and no page names to
claim, since each workspace owns its prefix. A half-written or broken page is the
workspace's own business. The replies of `run-code`, `write-file` and `build-site` list the
pages they changed, with their URLs, what in them the CSP blocks and their notices
(scripts, and the sandbox's limits on them). Caddy's directory
listing is the index, of the workspaces at the root and of a workspace's pages under it;
dotfiles aren't served.

What keeps this safe is where `/public` lives: in `~/.local/share/everythingllm/sandbox/public/<workspace>/`,
apart from the workspace's other folders, in a tree that holds nothing but `/public`
folders. Caddy mounts that tree read-only, so a symlink in a workspace's pages can only
reach other workspaces' pages (public already) or Caddy's own container, never a
workspace's `/project`, `/work` or `/shared`. `/public` counts toward the workspace's size
limit.

**Link cards.** AnythingLLM's chat shows a Markdown image up to 800 px wide, and keeps it a
link when it's inside one, even with "Render HTML in chat" off. That setting is per browser
and off by default, and the HTML it lets through is sanitized (DOMPurify: no scripts,
handlers or iframes), so anything richer than text that has to show wherever the chat does
is a picture the host draws: `packages/chatimage`, whose link cards are one kind and deep
research's live progress cards another. Whatever publishes a page
(`publish`, `write-entry`, deep research) has `chatimage.card` draw a card of
it (its title, its site or workspace, a line about it, its address) into `_cards/` on the
pages site, and adds a `Card: [![title](card.png?v=…)](page)` line to its reply, which the
system prompt has the agent paste as is. Pages that are already there have cards too:
`list_sites` gives one per site's home page, `list_entries` one for the newest entry and
`get_entry` the entry's, and the system prompt has the agent link a site or an entry through
them rather than from memory. The `?v=` is a hash of what the card says, kept in the PNG
too, so an unchanged card isn't redrawn and a changed one gets a new URL the chat hasn't
cached. Removing a page or deleting an entry deletes its card.

Code never runs in the AnythingLLM container, which has SYS_ADMIN, the `.env` keys and all
of storage. The skills (`anythingllm/agent-skills/`, sharing `_lib/`) only forward calls
over a Unix socket, `storage/everythingllm/sandbox/runner.sock` (see "Services on the host"), to
`sandbox-runner` on the host (`host/systemd/sandbox-runner.service`, its own venv in
`~/.local/share/everythingllm/venvs/sandbox`).

**Scopes.** A call carries where it came from, which AnythingLLM gives the skill and the
model never chooses: the workspace (`_jobs` for a scheduled job, which has none) and the
chat thread (`default` for a workspace's main chat, and for API, Telegram and job runs).
A call through the MCP gateway carries the workspace `client-<name>`, from the client's
token, the thread `gateway` and `gateway: true` (see "MCP gateway"). Workspaces whose names
start `client-` are kept for those: the runner refuses one in a scope that doesn't say
`gateway`, so an AnythingLLM workspace slugged `client-…` gets a message to rename it
rather than a gateway client's folders. Each run mounts:

- `/work`: the thread's scratch folder, and where a run starts. It's deleted 7 days after
  the thread last used the sandbox.
- `/project`: the workspace's folder, shared by its threads and kept until deleted. `pip
  install`s go to `/project/.local`, so they last too. To keep a file, move it here.
- `/shared/<workspace>`: what the workspace shares with the others, kept until deleted. It
  writes it; every other workspace's runs mount it read-only at `/shared/<that workspace>`.
  Nothing is written by more than one workspace, so a prompt injection in one chat can't
  change what other workspaces use; reading another workspace's folder is still trusting
  its content, which the skills and system prompt tell the agent to treat as data. Shared
  folders are mounted `noexec,nosuid,nodev` and are never on `PATH`.
- `/system/themes`: the repo's Zola themes (`packages/sites/zola/themes`), read-only, for sites the agent
  builds.
- `/public`: the workspace's pages on the web (see "Pages are `/public`").

**Chat attachments.** A file attached in an AnythingLLM chat is in that chat's
`/work/attachments/` as text, for `run-code` to read rather than the agent pasting it into
a script. AnythingLLM keeps no attached file, only the text it made of it
(`storage/direct-uploads/<name>-<uuid>.json`, and a row in its `workspace_parsed_files`
table), so `.csv`, `.tsv`, `.txt`, `.md` and `.json` keep their names and anything else
becomes `<name>.txt` (a PDF's text; a spreadsheet's sheets as CSV, their names in the file's).
`run-code` looks the chat's attachments up in AnythingLLM's database, through the server's
own Prisma client (`_lib/attachments.js`: the skill runs in AnythingLLM's server), and
sends the runner their titles and file names, at most 50. API, Telegram and job runs have
no chat and send none; the gateway's `sandbox_run` never does, and the runner ignores them
for a gateway scope. Before the run, under the workspace's lock, the runner reads each new
one from `SANDBOX_UPLOADS` (storage's `direct-uploads`) without following a symlink, at
most 50 MB a file and 200 MB a run, and writes its text; `.manifest.json` beside the copies
records each one's source and hash. A copy already there stays as it is, edited or not.
When the lookup was whole, a copy of a file no longer attached is removed if it's
unchanged, and an edited one stays as the chat's own; a failed lookup removes nothing, and
neither does an attachment whose text is gone. Copies count toward the workspace's limit
(past it, they're left out with a note), aren't among the files a run changed, and the
reply names them.

They live in `~/.local/share/everythingllm/sandbox/workspaces/<workspace>/` (`threads/<thread>/`,
`project/` and `shared/`), out of the container's reach. The runner's own file operations
(`write-file`, `publish`) only take paths in the caller's own folders, never another
workspace's. A workspace's folders together are held to 5 GB: over that, runs and writes are
refused until the agent deletes something with `write-file`, and the refusal names the
biggest files and folders, since no run can look for them. A run warns past 4 GB, and one
that takes the workspace past 6 GB or 200,000 files while it goes is killed (the runner
looks every 3 s), so a run can't fill the host's disk. Runs in
one workspace take turns, since they share `/project`; while one is going, a write or
publish from any of the workspace's chats fails at once rather than waiting. Runs in
different workspaces overlap. `docs/.proposals/shared-sites.md` (kept out of git) has the design.

**The lab site** is the one site the agent controls entirely: templates, stylesheets,
`zola.toml` and content, in education's `/shared/education/sites/lab/`, where other
workspaces can read it and copy it. It started as a copy of the `agent-site` theme and a
welcome entry, with a `README.md` for the agent and a git repository so it can roll back.
It's built with `build-site` (`path` `/shared/education/sites/lab`, slug `lab`), which
puts it at `https://<PUBLIC_HOST>:8447/education/lab/`. Nothing in the repo or on the host reads it,
so it can break without breaking anything else, and the CSP and its sandbox still hold
for whatever it serves.

**Building sites.** `build-site` (`op_build_site`) builds a Zola site from a folder in the
workspace's own `/project`, `/shared/<workspace>` or `/work` (the folder's name is the
slug unless one is given). The build runs `packages/sandbox/src/sandbox/sitebuild.py`,
copied from the repo into the run's read-only `/sandbox`, so nothing in a workspace's
folders can change what a build runs, in a container with no network at all and every
folder read-only but an empty `/out`:
- it copies the site to `/tmp`, leaving out `.git` and an old `public/`;
- it puts the theme named in `zola.toml` in place: with `[extra.build] theme_from =
  "system"` the repo's from `/system/themes`, with `theme_from = "<workspace>"` that
  workspace's `/shared/<workspace>/themes/<theme>`, and without it the site's own
  `themes/`. Another workspace's theme comes in without its symlinks, and can't itself be
  one: zola copies static files through a symlink, so a theme's `static/x -> /project`
  would otherwise publish the building workspace's private files (zola already keeps
  `load_data` inside the site);
- it runs `zola build` with the base URL the runner passes in
  (`…:8447/<workspace>/<slug>`), so a site can't point its links at another host, within 60 s.

The runner copies the output into `/public/<slug>` (plain files only, in place of what was
there), so a site is live like any page; zola's error comes back if it doesn't build, and
nothing changes then. A build waits like a run (`op_wait`).

**Each run** gets a fresh `localhost/everythingllm-sandbox` container, with the script mounted
read-only from a host-only folder at `/sandbox`:

- non-root (`--userns keep-id`), read-only root, `--cap-drop ALL`, `no-new-privileges`;
- 1 CPU, 1 GB memory, 256 processes, 4096 open files, no file over 2 GB, 60 s by
  default (300 s max), then killed; a run
  that hits the memory limit is reported as such (podman's `OOMKilled`);
- output clipped to the first and last part; at most 2 runs at once;
- containers carry the label `everythingllm-sandbox=1`; the runner removes any left over
  from a crash or restart when it starts.

The host never follows a symlink out of a mount when it reads, writes or copies for the
agent, won't write into a FIFO or device there, and leaves symlinks out of what `publish`
or a build copies into `/public`; the sandbox can create any symlink it likes in its own
folders.

**Network.** A run sits on `egress-net` (see "Service containers"), with no route out and
no DNS (`--dns none`), at one of the egress profile `sandbox`'s addresses (`10.89.79.40`,
`.41`): each is a slot, held from writing the run's script until its container is removed,
so at most two run at once and an address is never handed on while a stopped container
still has it (a build, with no network, holds a slot too, for its turn). The runner points
`http_proxy` and `https_proxy` at the egress proxy (`:3128`), whose `sandbox` profile has
no `public` and no exceptions of its own, only what every profile may reach:
`pypi.org:443` and `files.pythonhosted.org:443`. So `pip install` works, and the internet,
the LAN, CGNAT (a tailnet's AnythingLLM API, Ollama, …) and the host's own ports don't.
`upload.pypi.org` stays blocked, so code can't push data out through a package upload
either. To allow another host, add it to the profile's `allow` in `egress.toml` and
restart `egress-proxy`.

`uv run hostctl health` checks the unit and pings the runner, which reports a missing
image or network and an egress proxy that isn't running.

## Browser

Each workspace has a browser of its own, a real Chromium that the agent drives and you can
watch and take over, like the browser in Meta's Muse but split by workspace: a login made in
`career` is there for every chat in `career` and never for `education`. Four skills drive it:

- `browse` opens an address in this chat's tab and replies with the page as text: its
  interactive elements, each with a ref (`[e12] button "Sign in"`), then its visible text,
  under a line saying it's the page's own, untrusted content. The first time, it also gives
  the tab's live card.
- `browser-act` does one thing to an element by its ref (click, fill, type, press, select,
  check, hover, scroll, back, forward, reload, wait) and replies with the page after.
  `press` sends plain keys only (Enter, Tab, an arrow, a character, Shift with Tab or an
  arrow), never a Control, Meta or Alt shortcut, so nothing goes through the clipboard.
- `browser-read` reads the page again, or only its lines that contain `find`.
- `browser-handoff` gives you the browser (to log in, enter a 2FA code, solve a CAPTCHA, pay)
  and replies at once with the card. The agent puts the card in its reply and ends the reply,
  since a skill that waited would keep the card out of the chat. Until you hand the browser
  back, its actions are refused. Hand it back in the take-over view, or tell the agent you're
  done, and it calls `browser-handoff` with `done: true`.

- `browser-login` logs in with a login or passkey saved in the workspace's vault, without
  the agent ever seeing it, or asks you for a login on a card (see "Saved logins" below).

They're skills, not MCP tools, because they act and must know their workspace: each call's
scope is `{workspace, thread}` from AnythingLLM's invocation (`_lib/scope.js`, as the
sandbox's), never from the model, and each refuses a delegated task. Gateway clients get no
browser.

**The card.** A tab's card is a live picture of it in the chat:
`https://<PUBLIC_HOST>:8445/_live/browser/<id>.jpg`, served by browser-runner on :8453
(`browser.live`, routed by the machine like the research cards). It's the tab's screenshot under
a strip saying who has the browser (the agent, you, or closed), the page's title and
address, and what was done last ("Clicked e12"; what's typed is never shown). It's pushed
again (`multipart/x-mixed-replace`, as JPEG) whenever the tab looks different, checked once a
second while someone watches. A watched card keeps the browser from being stopped as idle.
A closed tab shows its last look, dimmed, and the card wakes up when the tab is used again;
the same chat keeps the same tab and card from one container to the next. The tab's id is
`bw-` and 16 hex digits, so the card is the way to it.

**The take-over view.** The card links to `https://<PUBLIC_HOST>:8454/<token>/` (through a
redirect from :8445, since the token changes with each container), a page of its own on its
own HTTPS port (`browser.takeover`, :8454), so its scripts run on an origin of their own
and not the pages site's. It shows the browser's whole screen through noVNC (`static/app.js`),
view-only while the agent has it. "Take over" makes it yours: the agent's actions and reads
are refused (so it can't watch what you type) until you press "Hand back to the agent". When
it comes back, whatever is in a password field, sent or not, is hidden from its reads as a
filled secret is, and so is a login you sent. The VNC stream reaches the page over a WebSocket
the runner carries to x11vnc's Unix socket (`browser.websocket`); nothing in the container
listens on a port. A POST or a WebSocket must come from the page's own origin. noVNC's files
come from the browser image (`uv run hostctl browser-images` copies `/opt/novnc` to
`~/.local/share/everythingllm/browser/novnc/`), so the page and the image's x11vnc are from one
build.

**Saved logins.** Like Muse's credential vault, the workspace's logins are the agent's to
use and never to read. They live in browser-runner, outside both the agent and the browser
(`browser.vault`): one file per workspace, `~/.local/share/everythingllm/browser/vault/<workspace>.vault`
(0600), sealed with AES-GCM under a key kept apart from the data dir and its backups,
`~/.config/everythingllm/browser-vault.key` (made on first use, 0600), with the workspace's
name bound in so one workspace's file can't stand in for another's.

- **Using one.** `browser-login` lists the logins (id, site, username, whether it has 2FA
  and whether it asks first; never a password or 2FA secret) and which fit this chat's
  page. The agent names a login and the fields from its last read; the runner sends the
  secret to the driver, which types it in. It never comes back in a reply, a log or the
  card, and the agent never types a password itself. Once it's in, the agent can't read
  it back by the page's "show password" button and an edit: a read hides any six
  characters of a filled secret wherever they show (`driver.hide`), and a field holding
  one can only be submitted, left or replaced, never typed into, trimmed or selected.
  Passwords stay hidden for the container's life (only 2FA codes, which go stale, are let
  go after 20). As a read hides what the agent sends too, a guess sent and seen hidden would
  spell a secret out, so an address or text the agent sends (or a run of its key presses)
  that holds a piece of one is refused, and the browser is locked to it until you take it
  over in the view yourself.
- **Only on its own site.** A login is saved for a site (`linkedin.com`: the host, without
  `www.`) and fills only there or on a subdomain (`browser.origin`), checked by the runner
  against the tab and again by the driver against the frame the field is really in, and a
  password goes only into a password field. So a page that talks the agent into it can't
  have your LinkedIn password typed into another site. A site is never a public suffix
  (`github.io`, `co.uk`, from the Public Suffix List kept in `browser/public_suffix_list.dat`),
  whose subdomains belong to anyone, and a login never fills across one below its site
  (one for `windows.net` not on `anyone.blob.core.windows.net`), nor is a name that only
  means something locally (`printer.local`, `nas.lan`, a dotted number). It fills only on
  an https page and frame on the usual port, never in the clear. From the first password
  filled, every request the browser makes is checked for the passwords it holds (as typed,
  URL-encoded or in JSON, `driver.leak`), and one carrying a password anywhere but its own
  site over https is blocked and said in the next read: a form whose action points
  elsewhere, or a script, can't send it on.
- **2FA.** A login can carry a TOTP secret (the text under the QR code, or its
  `otpauth://` address); `browser-login` with `code` fills the current code. That puts both
  factors in one vault on this machine; leave the secret out for accounts where that's too much.
- **Asking first.** A login marked "ask me before each use" makes the agent wait for your
  OK: the card says so, and the take-over view shows "The agent wants to use your login for
  …, on <the page's address>" with Allow and Don't allow. An OK lasts 10 minutes for that
  login in that chat alone (`GRANT`), long enough for the password and the code; another
  chat asks again. The skill waits up to 5 minutes, then has the agent ask.
- **Adding one.** Never through the chat, where the model would see it. The take-over
  view has a Saved logins panel to add, list, mark and delete them; it can save and delete,
  never show a password. And while you have the browser (you took over, or the agent handed
  it to you), a form you send with a password in it is offered for saving there ("Save the
  login you just used on …?"), with the site taken from the frame it came from, whatever the
  page says (`capture.js`). Offers last 10 minutes and are only ever made while you have it.
- **Asking for one.** When there's no login for the site, `browser-login` with `ask` gives
  the agent a card for its reply, "Log in to <who the site belongs to>" (`google.com` for
  `accounts.google.com`, `evil.app` for `accounts.google.com.verify.evil.app`, the name
  above the public suffix, `origin.registrable`, so a long host can't push the real owner
  out of sight)
  (`https://<PUBLIC_HOST>:8445/_live/browser/login/<id>.png`, drawn like a progress card and
  pushed again as it's answered). It links to a page of its own in the take-over view,
  `/login/<id>/` on :8454: a form with nothing else on it (no noVNC, so it works on a
  phone) for a username, password and optional 2FA secret, which go into the vault. The
  runner names the site from the chat's page (`Runner.op_ask_login`), never the model, and
  the form lets you widen it only to a parent short of a public suffix
  (`accounts.google.com` or `google.com`); it shows the page's address too, so a page that
  talks the agent into asking can only ask for its own site's login, in plain sight, and
  warns when no login in the workspace is for that owner yet. The
  request's id (`lr-` and 32 hex digits) is the page's only key, so it needs no token and
  outlives the browser; it waits 30 minutes (`ASK_SECONDS`), takes one answer, and the
  take-over view lists the waiting ones. You tell the agent in the chat once it's saved,
  and it logs in with it as with any other.
- **Passkeys.** The vault keeps passkeys too, beside the logins (each entry has a `kind`;
  the vault keeps only what the runner has a way to use without the agent reading it, so
  it isn't a store for API keys). Only you make one: take over, press "Make a passkey" in
  the Saved logins panel, then add a passkey on the site's page. Every page then has a
  virtual authenticator (Chromium's WebAuthn over CDP, which no page can reach) until one is
  made, 5 minutes pass or you hand back. What a site makes is saved, asking first since
  nobody touches a key when it signs in, as the panel next refreshes (or, with it closed,
  on the hand-back or as the browser stops), as a passkey the site has and the vault
  doesn't is one nobody can use. The
  agent signs in with `browser-login` `passkey`, naming the passkey and the page's button
  for it: the runner and the driver check the page is on the passkey's site over https, as
  for a login, the driver puts an authenticator holding only that passkey in the page for
  the click and at most 15 s after (`PASSKEY_SECONDS`), and Chromium itself checks the page
  may use it. It covers the chat's page, not a popup or a frame of another origin. A passkey
  can't come from your phone or password manager (they don't give theirs out), it's this
  machine's alone, so keep another way into the account, and a site that demands an
  attested authenticator (some banks, work accounts) refuses it.
- Chromium's own password saving is off in every profile, so what you type stays out of
  the profile. `browser-reset` wipes a profile but leaves the workspace's saved logins;
  delete those in the panel.

**Where things are.** A workspace's browser is a container,
`everythingllm-browser-<workspace>` (image `localhost/everythingllm-browser`,
`host/containers/browser/`). It's started on the workspace's first call and stopped after 20
minutes unused and unwatched; the profile outlives it:

    sandbox/workspaces/<workspace>/browser/profile/   cookies, logins, history (mounted at /profile)
    browser/downloads/<workspace>/<thread>/           downloads as they're saved (/downloads)
    sandbox/workspaces/<workspace>/project/downloads/ where they end up (run-code's /project/downloads)
    browser/sockets/<slot>/                           driver.sock and vnc.sock (/run/browser)

The profile sits in the workspace's sandbox folder, beside the folders the sandbox mounts,
never in one: no run can read the cookies, and the sandbox's size limit leaves the profile
out. The container never mounts a folder a run can write, since a run could make it a
symlink and podman would mount wherever it points. Downloads are saved to browser-runner's
own folder (at most 10 between two reads, 256 MB each), and the runner copies each one to
`/project/downloads` after the thread's next call, opening every step without following a
symlink (`hostrpc.safefs`). They go to `/project`, not `/shared`, which every other
workspace can read.
`uv run hostctl browser-reset <workspace>` stops the workspace's browser and wipes its
profile.

**The container.** It's hardened like a service container: read-only root (`/tmp` and
Xvfb's key maps on tmpfs), every capability dropped, `no-new-privileges`, `keep-id`, 2 GB of
memory, 1 CPU, 1024 processes, `--init`, and the repo mounted read-only for the driver's
code. Its entrypoint starts Xvfb, x11vnc on `/run/browser/vnc.sock`, and `browser.driver`,
which launches Chromium through Playwright with the persistent profile and answers
browser-runner on `/run/browser/driver.sock` (hostrpc). One tab per chat thread; a popup
(a login window) becomes the thread's tab until it closes; downloads are saved and alerts
answered on their own (confirms are dismissed), and both are reported in the next read.
Closing the window ends the container, and browser-runner starts it again on the next call.
Chromium's own sandbox is off (it needs user namespaces the container doesn't give), so the
container is the boundary.

**Network.** The container is on egress-net with `--dns none`, at one of the four addresses
of the `browser` profile in `egress.toml` (`10.89.79.32`–`.35`: the slot it holds while it
runs, so at most four browsers run at once; when a fifth is needed, the one unused longest is
stopped, unless it's watched or yours). Chromium sends everything, loopback included,
through the egress proxy's public port (`:3129`): public hosts on 80 and 443, never
CGNAT, the LAN or this machine, and not even PyPI. A renderer taken over by a page could
reach other containers on egress-net directly, as any service container could; their
servers answer only loopback and their own address (`hostrpc.local_peer`). The proxy loads
`egress.toml` when it starts, so a new profile or address needs `uv run hostctl
egress-setup` (it asks while a deep-research run is going).

**Mind what it's logged into.** Pages the agent opens can try to instruct it (prompt
injection), and the agent has your other tools too. Log the browser only into accounts
you'd let it use unsupervised; never email, banking or a password manager. The agent is
told to hand over for logins rather than type passwords, and to ask before anything it
can't take back.

`uv run hostctl browser-setup` builds the image (`browser-images`), maps :8445/_live/browser
and :8454, and starts `browser-runner` (`host/systemd/browser-runner.service`). Restarting
the runner stops every browser (the profiles stay).

## News articles

A Daily News headline doesn't link to the outside source. It opens an article the bot
writes the first time someone clicks it. The edition links each story to
`/news/write/<day>/<desk>/<n>` (the n-th story of the edition's desk-th section), which
the machine routes to the article writer (`sites.articles_web`, served by `sites-runner`
on 127.0.0.1:8448, its container's published port), so the writer shares the news site's origin while Caddy stays static
and read-only. Without a DeepSeek key, `sites-runner` logs that and serves the tools without it.

- If the article is already written, the writer redirects to it at
  `/news/articles/<desk>-<n>-<day>/`. Otherwise it starts writing and answers a page that
  reloads every 4 s until the article is ready (no script, so no CSP problems). Writing
  takes about 5–30 s.
- A link only names a story the daily job saved, by edition day and positions. The
  writer never fetches a URL taken from the request.
- To write a story, it searches SearXNG (`https://<PUBLIC_HOST>:8888`, through the egress
  proxy; 127.0.0.1:8888 on the host) for the headline, since the job
  often saves a site's front page as the link, and reads the story's link plus the top
  results with trafilatura (up to 4 readable pages). Only public hosts are fetched,
  redirects included. DeepSeek (AnythingLLM's model, thinking off) then writes 250–600 words from the pages that actually report the story,
  or refuses if none do. Its key and model are read from AnythingLLM's `.env`, and only
  those, once when `sites-runner` starts (the container mounts its share of the file,
  written as it starts), so a new key or model takes `uv run hostctl sites-setup`.
- The article is an ordinary `sites` entry in the `articles` section, saved and built from
  the host like `uv run hostctl sites-build`. It ends with "Based on reporting by …", linking the
  pages it used. It's rewritten only if the edition's story changes.
- A failure (nothing readable, no page about the story, an API error) shows a page with
  the error, a link to the original and "try again". Without that it's retried
  automatically after 2 minutes. At most 2 articles are written at once.
- Articles are also in the site's Atom feed, since Zola's feed covers every section.

## Deep research

`deep-research` does what Claude's research mode does, within one agent tool call (the
agent itself is capped at 40 tool calls per reply, `AGENT_MAX_TOOL_CALLS` in `.env`, so the
work happens outside the agent). The skill (`anythingllm/agent-skills/deep-research/`) is a
thin front: it hands the question, its setup args and the workspace to `research-runner`
(`packages/research`), which runs in a service container of its own
(`host/quadlet/research-runner.container.in`, see "Its container" below and "Service
containers"), and answers at once with the run's live
progress card, so the chat is free while the run goes. They talk over a Unix socket the
container sees, `storage/everythingllm/research/runner.sock` (see "Services on the host"):
`start` returns a run id and its card, `wait(run_id, since)` long-polls up to 45 s for new
progress lines and the result, `runs` lists what the runner holds.

**The live card.** `start`'s `card` is a Markdown image in a link,
`[![Deep research: <question>](…/_live/research/<id>.png)](…/_live/research/<id>)`, which the
agent pastes as it does a link card. research-runner serves both on 127.0.0.1:8450
(`RESEARCH_LIVE_PORT`, `research.live`; its container publishes the port there), which the machine routes
`https://<PUBLIC_HOST>:8445/_live/research/` to. The image is
`multipart/x-mixed-replace` (server push, `chatimage.live`): the browser keeps showing the
newest frame of the connection, so the card's bar, its minutes and its latest progress line
move with the run, with no script and with "Render HTML in chat" off. A frame goes out at
most once a second, when the run moves on; the response ends with the run (green when it
published, red when it failed) or after 30 minutes, and a reload asks again. The bar is how
far along the run is (`meter` in `job.run` and the pipeline): planning, then the searches
against the depth's budget for most of it, then writing, fact-checking and publishing.
Someone watching the card counts as following the run, as the skill's wait used to. The
link opens the report once it's published, and until then a page of the run's latest
progress lines that reloads itself. Each run's line in the run log keeps its `run_id` and
`card`, so a run the runner no longer holds (an hour after it ended, or after a restart)
gets one frame of how it ended from the log, and an old chat's card still opens the report.

A run, step by step:

1. **Plan** — the planner model splits the question into sub-questions with search queries.
   The calling agent can make the split itself instead: the skill's `sub_questions` (each a
   goal, or `{goal, queries}`, at most the depth's workers) and an optional `title` skip
   this step, and the run log's `stats.plan` says `caller`.
2. **Research** — one worker per sub-question, all in parallel. Each searches SearXNG
   (`SEARXNG_URL`: `https://<PUBLIC_HOST>:8888/search` from the container,
   `http://127.0.0.1:8888/search` on the host), reads pages with
   publicweb's page reader, as the article writer does (`publicweb.pages`: browser-like
   headers, trafilatura, public hosts only, redirects included, HTML only, so PDFs are
   skipped) and extracts findings as claim + verbatim quote. Findings whose quote isn't actually on the page are dropped.
3. **Gap check** — the planner reviews all findings and sends out follow-up workers for
   gaps and contradictions. If the check fails, the run goes on to writing.
4. **Write** — the planner writes a Markdown report from the findings only, citing [n].
   If that fails, the findings, grouped by sub-question and cited, become the report
   (`stats.write` in the run log says so).
5. **Fact-check** — the planner flags sentences the cited notes don't support; the
   edits are applied in code, citations are renumbered and the source list appended.
6. **Publish** — the report is first saved as `storage/anythingllm-fs/research/<slug>.md`
   (slug from the title, as sites-write makes it), where the agent's filesystem tools can
   read it, then saved and built in `~/.local/share/everythingllm/pages/entries/research/reports/` through `SiteStore`,
   as the `sites` server does; the live card turns green and links to it. If publishing
   fails (the site doesn't keep an entry it couldn't build), the card says so and the run
   log has the saved file and the error. A file that couldn't be saved only warns: the
   reply and the run log (`file_error`) say so. That's all a run keeps: the report isn't
   added to a workspace's documents, and the agent finds it with `sites list_entries`.

Depth (`quick` / `standard` / `thorough`, default standard) sets workers, steps per worker,
gap rounds and a search budget (15 / 40 / 80); see `packages/research/src/research/config.py`. Models are setup args:
`PLANNER_MODEL` (`glm-5.3`) and `WORKER_MODEL` (`deepseek-flash`, run with thinking
off). A `glm-*` model goes to Z.AI's coding endpoint (a GLM Coding Plan key gets "1113
Insufficient balance" anywhere else), with the Generic OpenAI provider's key when its base
path is Z.AI's, else `ZAI_API_KEY`; any other model goes to DeepSeek. The runner reads
those keys from AnythingLLM's `.env` for each run (`llm.provider`), and only those. The planner makes a
handful of large calls and the workers make the many small ones (263 calls in a thorough
run), so the plan's usage limits go to the planning and writing, and DeepSeek's per-token
pricing to the bulk. The worker must be a DeepSeek model: `glm-5.3` can't turn thinking
off. When Z.AI says the plan's usage is spent (429, or 1113 "Insufficient
balance"), the planner switches to `PLANNER_FALLBACK_MODEL` (`deepseek-flash`, with thinking
on; `off` turns this off) for the rest of the run; the chat's progress says so, and the run log's
`stats.fallbacks` records it. The planner was `deepseek-v4-pro` until 2026-10-04, when that was V4.1-Flash
underneath (see below).

DeepSeek notes, found while building it:
- Don't use JSON mode (`response_format`): with it, flash often replies with the wrong keys
  or just `{"type": "json_object"}`. Plain prompts plus a parse/repair retry are reliable.
- Thinking tokens count against `max_tokens`, so planner calls get large budgets
  (16k for JSON, 64k for writing and fact-checking; the API allows up to 393,216).
- Since 2026-09-14 DeepSeek serves `deepseek-v4-pro` requests with V4.1-Flash, until
  V4.1-Pro launches.

Measured runs (2026-10-03):

| depth | time | searches | pages read | findings kept / dropped | sources cited | LLM calls | tokens in / out |
|---|---|---|---|---|---|---|---|
| quick | 5 min | 3 | 11 | 59 / 6 | 8 | 29 | 58k / 41k |
| thorough | 11 min | 108 | 73 | 268 / 31 | 25 | 263 | 473k / 86k |
| standard (paced, after the search budget) | 9 min | 18 | 46 | 219 / 12 | 25 | 109 | 267k / 75k |

Search is the limit, not DeepSeek. The thorough run's 108 searches got Google CSE (where
most results come from) suspended for "too many requests" along with Brave and
DuckDuckGo, and SearXNG returned nothing until they recovered. Since then:
- searches go out one at a time, at least 2 s apart (page reads and model calls stay
  parallel), and each run has a search budget by depth; once it's used up, workers read
  from the results they have and further gap checks are skipped;
- when SearXNG returns nothing because engines refused it, the run fails with "Web
  search isn't working" and names the engines; workers stop after two failed searches
  in a row so they don't prolong the block.
More engines in SearXNG's settings (Ansible) keep search working when one blocks us.

A run doesn't stop when its chat closes, or when AnythingLLM restarts: it belongs to the
runner, which publishes the report as usual. A run nobody was watching (its
card, or a `wait`) when it finished gets `chat_closed: true` in the run log. To
find the report, ask the agent (it looks with `sites list_entries`) or open the research site. A run can't be
cancelled from the chat: `FORCE=1 uv run hostctl research-setup` restarts the runner, which kills
every run in it. Runs are bounded by their search budget either way. At most 2 run at once;
another waits its turn, and its progress says so.

**Telling the Nilson app.** The skill sends the chat it was called from (`_lib/scope.js`'s
workspace and thread id) with `start`. When a run from a workspace's chat ends, and
`NTFY_URL` is set in `~/.config/everythingllm/relay.env`, research-runner posts to the
relay's ntfy topic (`research.notify`): "Research ready" or "Research failed", the
question's first 120 characters, `run=dr-…,workspace=…,thread=…` as its tags and the
report's URL as its `Click`; never the report. The thread is AnythingLLM's numeric thread
id (`/api/v1` gives clients slugs only), so the app finds the chat by the run id in the card
it drew. A gateway client's run and a scheduled job's (`_jobs`) tell no one.

Only a restart of `research-runner` kills a run without a result, so while a run is going
it has a marker in `~/.local/share/everythingllm/research/runs/running/<id>.json` (its question and when it
started), touched every minute. When the runner starts, it moves every marker into the
log as status `interrupted`, since none of them can be its own; until then, a marker quiet
for its `stale_ms` (3 minutes) belongs to a run that's gone too (`hostctl.run_guard` reads
it so), and a fresh one to a run that's going.
`uv run hostctl research-setup` and `uv run hostctl units` (when the unit changed) list the live runs and ask
before restarting the runner; with no terminal to ask they stop, unless `FORCE=1`
(`hostctl.run_guard`). `uv run hostctl restart` and `uv run hostctl deploy` restart AnythingLLM only,
so they don't need to ask. The runner runs the code it started with: after changing
`packages/research`, `uv run hostctl research-setup` puts it live.

Every run appends one line to `~/.local/share/everythingllm/research/runs/YYYY-MM.jsonl`: the question,
how it ended (`ok` / `failed`, with the error; `interrupted` for one killed by a restart;
older runs may say `stopped`), whether the
chat closed before it finished (`chat_closed`), the report URL and whether it
published, its stats (including `tokens`, with `cached` the input the provider served from
its prefix cache, `fact_check` and per-worker `workers_detail` with why
each stopped: `done`, `budget`, `wasted`, `search-down`, `notes-full`) and every progress
line. AnythingLLM keeps only a chat's final reply, so this is the run's record.

**Its container.** research-runner runs in `localhost/everythingllm-service`, hardened as
every service container is (see "Service containers"), with 2 GB, 2 CPUs and 256 PIDs (two
runs of a few dozen threads each) and `Nice=10`, which podman passes on to it. Its venv is
`venvs/research-runner-ctr/`; the first start syncs it from PyPI through the egress proxy,
so the socket and the live cards come up a few minutes later that once. It sees, each at its
host path:

- the repo, read-only: the code, the sites' sources and `host.env`
- in the data dir: `research/` (the run log); `pages/entries/research/` and
  `pages/entries/.build.lock`, the lock every site build holds, so its builds and
  sites-runner's still take turns; and `pages/public/` whole, since the sandbox builds the
  research site into `.research.new` there and the rename into place must stay within one
  mount (the link cards go in its `_cards/`)
- in storage: its socket folder; the sandbox's build socket's (`sandbox-build/`, which
  serves only `build_system_site`), read-only (connecting needs no more): the research site has `theme_from = "system"`, so no zola runs in the
  container; `anythingllm-fs/research/`
- its share of AnythingLLM's `.env` (`~/.config/everythingllm/ctr/research-runner.env`),
  read-only: the DeepSeek and Z.AI keys and DeepSeek's model, never AnythingLLM's password

It gets the relay's `NTFY_URL` and `NTFY_TOKEN` as values (`EnvironmentFile=`), not the file.

It goes out only through the egress proxy, with the `research` profile: any public host
(the pages it reads, DeepSeek and Z.AI), SearXNG by `PUBLIC_HOST` (`SEARXNG_URL`) and the
ntfy host on :443 (`NTFY_HOST`, for a self-hosted one), never AnythingLLM. A page the proxy refuses
(a LAN or CGNAT address) is skipped as any unreadable page is. Only the research site's
entries are mounted, so a `SITE` setup arg naming another site can't publish there: the
report is still saved to the agent's files, and the reply says why. Its share of `.env` is
written when it starts, so a model key changed in AnythingLLM reaches it at its next
restart (`uv run hostctl research-setup`, with no run going).

To run one by hand, in this process rather than the runner (it logs and publishes as usual):

    set -a && . ./host.env && set +a && \
      uv run --package research research-run "Why is the sky blue?" --depth quick

## Delegation

`agents-runner` (`packages/agents`, `host/systemd/agents-runner.service`, its own venv in
`~/.local/share/everythingllm/venvs/agents`) runs **delegations**: a set of tasks the
caller defines, each done by AnythingLLM's own agent, headless, and an optional `then` task
that gets their replies (`docs/.proposals/agents.md`, kept out of git). The main agent
starts one with the `delegate` skill, for work that splits into parts that each need their
own searching or reading; reports stay with deep research, whose pipeline did the same job
for a hundredth of the cost when the two were compared. `agents-run` starts a delegation by
hand:

    set -a && . ./host.env && set +a && uv run --package agents agents-run \
      "Compare two heat pumps" --task a:worker:"Find the COP of model A, with sources" \
      --task b:worker:"Find the COP of model B, with sources" \
      --then planner:"Compare them in a short table"

`--material name:file` gives a task (or `then`) a file's text as its material, and
`--plain name` sends it as a plain chat.

Over its socket, `storage/everythingllm/agents/runner.sock`: `delegate(goal, tasks: [{name,
profile, instructions, material?, tools?}], then?)` answers at once with a run id and a live
card; `wait`, `runs` and `cancel` (tasks that haven't started won't; running ones finish,
unused). It also serves `update_prompt`, the scheduled jobs' `scheduled_jobs`,
`schedule_job` and `remind_once`, and `memories` (below), which only skills call.

- **Profiles are workspaces.** A task's `profile` is its role, and each role is an
  AnythingLLM workspace with its model and a system prompt (`agents/profiles.py`,
  `agents/prompts/`): `agents-planner` (GLM 5.3) plans, reviews and writes up;
  `agents-worker` (GLM 5 Turbo, which thinks least) searches and reads, with AnythingLLM's own web tools.
  Both are on the GLM plan, which AnythingLLM doesn't price.
  agents-runner makes and sets them through the developer API before its first delegation.
- **Each task** gets a thread of its own in its workspace, gone when the task ends, and at
  most `AGENTS_SLOTS` (3) run at once across all delegations. `then`'s prompt has the
  replies quoted in `<result>` tags as material, never instructions.
- **Material and plain chats.** A task's `material` (a draft, notes, findings; 200,000
  characters a task, 400,000 in all) goes into its prompt quoted in a `<material>` tag, as
  data. `tools: false` sends the task as a plain chat rather than to the agent, for
  judgment over what it's given. The result counts `tokens` per model as well as `cost`,
  since AnythingLLM has no price for generic-openai, the planner's provider.
- **Containment.** Every tool loads in a headless run, so every skill of ours that writes,
  acts or delegates refuses a call from an `agents-*` workspace (`_lib/delegated.js`, held
  by a test). AnythingLLM's built-in tools load there too, and our refusal doesn't reach
  them, so the ones that would let a task act later or plant text are kept from it by
  settings that the setup checklist checks (the end of `uv run hostctl install`, or
  `python3 -m hostctl.machine checklist` from `packages/hostctl/src`): create-scheduled-job is off
  (`schedule-job` makes jobs instead, and agents-runner disables a job that appears during
  a delegation; see "Scheduled jobs from a chat"), and it and the filesystem write tools
  aren't among the tools that run without asking. With those, a task can read and report;
  it can't write, run code, make a job or delegate again. Gmail's tools, if connected,
  are AnythingLLM's and load there as well.
- **The live card** is served on 127.0.0.1:8451 (`AGENTS_LIVE_PORT`) and routed by the machine from
  `https://<PUBLIC_HOST>:8445/_live/agents/`. Its page shows the
  progress, and every task's reply once the delegation is done, escaped and under a CSP
  that allows nothing but the page's own CSS (`runs.live`).
- **The run log** is `~/.local/share/everythingllm/agents/runs/` (`runs.runlog`, as
  research's).
- **The daily budget.** AnythingLLM's agent sends every page a task has read again with
  each step, so a task that reads a lot uses a lot of tokens (millions, for one that read
  eight pages), and a running task can't be stopped. On DeepSeek that was $0.20-0.60 a
  task, which is why both roles are on the GLM plan now; there it's the plan's usage
  limits that a big delegation runs into. For a profile on a priced model, agents-runner
  refuses a new delegation once those that started in the last 24 hours cost
  `AGENTS_DAILY_USD` (default 3; 0 turns it off), counted from the run log: AnythingLLM
  doesn't price GLM, and running delegations count once they end. The worker prompt asks
  for few page reads either way.
- **The key.** agents-runner calls AnythingLLM with a developer API key of its own, in
  `~/.config/everythingllm/agents.env` (`ANYTHINGLLM_API_KEY`, mode 600, put there by hand);
  `uv run hostctl agents-setup` checks it. Like research-runner, it isn't restarted by `uv run hostctl units`
  while a delegation is going (`hostctl.run_guard`).

### Scheduled jobs from a chat

A job runs its prompt with every tool approved, so only a chat may make one. AnythingLLM's
own tool for it, create-scheduled-job, is turned off: a delegated task can reach
AnythingLLM's built-in tools, which our refusal doesn't cover (see "Delegation",
Containment), and it can't list, delete or make a job that runs once either (its cron is
five fields in UTC, and a "one-off" set with it repeats every year). agents-runner does it
instead (`agents/jobs.py`) over AnythingLLM's internal API, logged in with its password
from storage's `.env` (`ANYTHINGLLM_ENV` names another), for three skills. Each refuses a
delegated task and a scheduled job's call, shows what it'd do, and acts only when called
again with `apply: true`, after the user agrees.

- **`scheduled-jobs`** (`action: list | delete | disable`, `id`, `apply`) lists every job:
  its cron (UTC), its next and last run in the user's time zone (`USER_TIMEZONE` in
  `host.env`, default Europe/Stockholm), the last run's status, and whether it's a one-off,
  and a missed one. Delete and disable take any job the repo doesn't manage (its names
  are `anythingllm/scheduled-jobs/*/job.json`'s), and never one with a run queued or
  going: AnythingLLM stops a running run when its job is deleted or changed.
- **`schedule-job`** (`name`, `prompt`, `schedule`, `tools`, `apply`) makes a recurring job
  on `schedule`, a five-field cron in UTC; the preview says how far the user's time zone is
  from UTC now, for the agent to show the times in it. It refuses a name in use, one of the
  repo's jobs' or starting `[once]`, and a tool as `remind-once` does.
- **`remind-once`** (`name`, `prompt`, `tools`, `at`, `apply`) makes `[once] <name>`, a job
  whose cron is that minute, day and month in UTC, from `at`, the user's local date-time.
  It refuses a time that has passed or is under a minute away, one more than 364 days
  ahead, one that doesn't exist or happens twice when the clocks change, a name in use,
  and a tool that `/api/scheduled-jobs/available-tools` doesn't list (or that needs
  setting up). Any tools may be given; the preview shows them, with the prompt and the time
  in both zones. The job's reply arrives as AnythingLLM's notification.
- **The registry and the poller.** A one-off made here is recorded in
  `~/.local/share/everythingllm/agents/once.json` (`{id, name, fire_at, state}`), and
  every 60 s agents-runner looks at those jobs, never one only named `[once] …`. Two
  minutes after `fire_at`, with no run queued or going, a completed run started at or after
  `fire_at` gets the job deleted (with its runs: AnythingLLM deletes them with the job). A
  job that never ran (missed, e.g. AnythingLLM was down) or whose run failed is disabled,
  since its cron would run it again a year on, then kept (its result stays readable),
  logged once and listed as such, and the agent offers to delete it. A run started by hand before `fire_at` doesn't count. While the registry is
  empty, the poller reads the file and nothing else.
- **The guard during delegations.** While a delegation runs, agents-runner lists the jobs
  every 10 s, and disables any job that wasn't there when it started and wasn't made by
  these skills, saying so in the delegation's events and its log: with create-scheduled-job
  off nothing else should make one, so this catches the tool turned on again. A job made
  by hand in the UI meanwhile is disabled too; turn it on again there.

### Saved memories

AnythingLLM keeps short facts about the user (Settings > Personalization): at most 5 global
and 20 per workspace, which it fills itself from idle chats and adds to every chat's system
prompt as "Things I Remember About You" (the global ones, and the 5 of the workspace's
closest to the chat). Its built-in `rag-memory` "store" isn't that: it embeds text into the
workspace's documents. Only the UI could manage them, so the agent didn't know it had them;
agents-runner does it over the internal API (`agents/memories.py`), logged in as for the
jobs above, for the **`memories`** skill (`action: list | save | forget`, `text`, `scope`,
`id`, `apply`), which refuses a delegated task and a scheduled job's call.

- **`list`** gives the global memories and the calling workspace's, each with its id and
  when a chat last got it, and the room left under each cap.
- **`save`** keeps one fact (at most 500 characters, no control characters) for the
  workspace, or with `scope: global` for every workspace, at once: the user asked, and
  `forget` undoes it. A full scope is AnythingLLM's refusal, passed on.
- **`forget`** takes only an id from the calling workspace's list (global or its own),
  shows the memory, and deletes it only when called again with `apply: true`.

With Personalization off, every action says so (AnythingLLM's "Personalization is
disabled.").

## MCP gateway

The gateway (`packages/gateway`, `gateway.service`, 127.0.0.1:8452, routed by the machine at https :8452)
serves the runners' tools over MCP's streamable HTTP to clients other than AnythingLLM,
such as Claude Code on another of your machines (`docs/.proposals/gateway-and-containers.md`,
kept out of git). It's one more front on the host, over the same runner sockets. Its tools
come in groups, and a client gets the groups it's granted. Two tools of the same name stop
it from starting.

- **The fronts' read tools** (group `sites`). It imports
  `sites.server` and serves its
  `tool.registered` (what `hostrpc.forwarder` registered), so the schemas and docstrings are
  the ones AnythingLLM sees.
- **The fronts' skills as tools** (`sites:write`):
  `write_entry` and `delete_entry`. The gateway wraps each front's `skills` (signatures, as its tools are) with
  `hostrpc.forwarder` itself, so each call goes to the front's runner under the op's name,
  as the generated skill's does.
- **Fronts declared in the gateway** (`agents`, `research`, `sandbox`): `gateway/agents.py`,
  `gateway/research.py` and `gateway/sandbox.py`, declared like a front's tools (signatures
  with docstrings) but never run as MCP servers of their own. Each names its tools with its
  `PREFIX` (`agents_`, `research_`, `sandbox_`), so their `wait`s and `runs` don't clash;
  the op sent to the runner keeps its own name (`delegate`, `start`, `run`, …).
- **Delegation** (`agents`): `agents_delegate`, `agents_wait`, `agents_runs` and
  `agents_cancel` over agents-runner. A client follows a run with `agents_wait`, advancing
  `since` by the events it got, until `done`. A client's delegations are its own: the
  gateway sends the runner the client as their owner (never from the arguments), and
  `agents_runs`, `agents_wait` and `agents_cancel` reach only those. The daily budget
  (`AGENTS_DAILY_USD`) counts these delegations too.
- **Deep research** (`research`): `research_start(question, depth, sub_questions, title)`,
  `research_wait(run_id, since)` and `research_runs()` over research-runner. A run started
  here takes the runner's defaults: its report is published to the research site and saved
  to the runner's files, and the models are the runner's, not the deep-research skill's
  setup args. `research_start` answers at once with `{run_id, queued, card}`; a
  client follows the run with `research_wait` as with `agents_wait`, and once it's done
  the result's `url` ends in the report's slug, which it reads with
  `get_entry(site="research", section="reports", slug)` (the `sites` group). A client's
  runs are its own, as its delegations are: `research_wait` and `research_runs` reach only
  the runs it started. Their reports aren't: they're on the research site, which every
  client granted `sites` reads.
- **The code sandbox** (`sandbox`): `sandbox_run(language, code, timeout)`,
  `sandbox_wait(run_id)`, `sandbox_write(path, content, delete)`,
  `sandbox_publish(slug, path, remove)` and `sandbox_build_site(path, slug)` over
  sandbox-runner, the ops behind `run-code`, `write-file`, `publish` and `build-site`. Each
  call carries the scope `{workspace: "client-<name>", thread: "gateway", gateway: true}`, made from the
  calling client's name (`gateway.grants.client`), never from the model's arguments: a
  `scope` argument is dropped, and the gateway's scope is the one sent. So a client has a
  sandbox workspace of its own, `client-<name>`, with one thread: its `/work` is
  `workspaces/client-<name>/threads/gateway`, and its pages are
  `https://<PUBLIC_HOST>:8447/client-<name>/`. A run or a build answers within the runner's
  45 s wait; one still going comes back as `{run_id, running: true, seconds}`, and the
  client calls `sandbox_wait` until it's done. A second 45 s wait wouldn't fit in the
  call's 55 s (hostrpc's call timeout), so no call runs past what an MCP client's own 60 s
  limit allows.
- **Sockets.** The fronts' `hostrpc.caller` falls back to the container's storage path, so
  at start the gateway sets each front's `<FRONT>_SOCKET` to the host's
  (`hostrpc.socket_path`), unless it's set already.

What the scopes don't do, by design (one user, so documented rather than enforced):

- A `client-<name>` sandbox workspace is a workspace like any other: its runs read every
  AnythingLLM workspace's `/shared/<workspace>` (read-only), and every workspace's runs
  read its `/shared/client-<name>`. Its `/project`, `/work` and `/public` are its own, and
  count toward its own size limit. The runner keeps `client-` workspaces for the gateway's
  scopes, so an AnythingLLM workspace slugged `client-<name>` is refused the sandbox (and
  told to rename) instead of sharing that client's folders.

**Clients and tokens.** Every path but `/health` needs `Authorization: Bearer <token>`. Each
client has its own token, a `GATEWAY_TOKEN_<NAME>` line in
`~/.config/everythingllm/gateway.env` (mode 600), and its name is `<name>` in lowercase,
`_` as `-`. A name is at most 63 letters, digits and hyphens, not starting or ending with a
hyphen, since it names the client's sandbox workspace too; the gateway won't start with a
token whose name isn't one. To revoke a client, delete its line and restart the gateway. The server is
stateless HTTP; DNS-rebinding protection allows only `127.0.0.1`, `localhost` and
`PUBLIC_HOST` as the Host.

**Grants.** `packages/gateway/src/gateway/grants.toml` (in the repo, beside the code, with no
tokens) gives each client its groups, `[clients.<name>] tools = ["sites", "agents", …]`.
`claude-code` gets every group. A client with a token but no grant gets no tools (the log
says so at start), and a key or group the file doesn't know stops the gateway from
starting. The gateway reads it at start, so restart it after a change. One MCP middleware,
`gateway.grants.Grants`, holds each client to its grant:

- it drops from `tools/list` the tools the client isn't granted;
- it refuses a `tools/call` outside the grant with an error naming the client (JSON-RPC
  `-32602`, as for an unknown tool);
- it logs each call with the client's name and the tool's, never its arguments or the
  token (a refusal as a warning), and tests hold that;
- it sets the ContextVar `gateway.grants.client` to the client's name around the call, so
  a tool can tell who is calling (the sandbox tools make the client's scope from it).

Why it may act where an MCP tool in AnythingLLM may not: writes are skills there because an
MCP call doesn't say which workspace made it, so it can't refuse a delegated task. A
gateway call is named by its token, and the client's grant says what it may do.

**Setting it up.** `uv run hostctl gateway-setup` makes `gateway.env` with a token for
`claude-code` when it's missing, maps the port, and starts the unit. It isn't part of
`uv run hostctl install`. It never prints a token.

**Adding a client.** `uv run hostctl gateway-client <name>` (`hostctl.gateway_env`) adds a
`GATEWAY_TOKEN_<NAME>` line with a fresh token when `gateway.env` has none for that client
(it never replaces one, and makes the file, mode 600, if there isn't one), then prints the
command to run on the client's machine, with `PUBLIC_HOST` from `host.env`:

    claude mcp add --transport http everythingllm https://<PUBLIC_HOST>:8452/mcp \
      --header 'Authorization: Bearer <the client's token>'

That's the one place a token is printed, so it's run on purpose and its output kept out of
anything shared. Run again, it prints the same command with the token the client already
has. It also says what `grants.toml` grants the client: a new client needs a
`[clients.<name>]` entry there before it gets any tools. Then restart the gateway, which
reads the tokens and grants only when it starts (`systemctl --user restart gateway`).
`uv run hostctl gateway-client claude-code` prints Claude Code's command after
`gateway-setup`.

`uv run hostctl gateway-logs` follows the gateway. A code change to a front's tools or
skills, to the gateway's own fronts or to `grants.toml` reaches it when it restarts;
`uv run hostctl deploy` doesn't restart it. A client already connected sees new or renamed
tools once it reconnects.

## Nilson relay

Nilson is a Flutter chat client (Linux desktop, Android) that talks to
AnythingLLM's developer API. Asked with `stream-chat`, AnythingLLM stops the answer when the
client disconnects and saves it to the thread only when the stream completes, so an answer
whose app closes, sleeps or loses its network is lost. On 2026-10-06, with AnythingLLM
1.16.2, an answer cut off after 15 chunks was missing from the thread three minutes later.

The relay (`packages/relay`, the `relay` service container, 127.0.0.1:8446) makes that one
call for Nilson and owns the answer. Each run streams from AnythingLLM to the end in its own
task, which no follower owns; the relay never closes the upstream connection because a
follower left, only when the run ends or is cancelled.

It sits beside AnythingLLM on AnythingLLM's own origin: the machine routes
`https://<PUBLIC_HOST>:3001/` to AnythingLLM as before and `/everythingllm/` on the same port
to the relay at `127.0.0.1:8446`; the relay answers with or without that prefix, so the
route may strip it or not. Everything else on :3001 (the web UI, both APIs, the websockets) is AnythingLLM's, so a
native AnythingLLM client notices nothing, and Nilson needs one address and one key for both:

- Every route but `/health` takes the AnythingLLM developer API key the client gives
  AnythingLLM itself, as `Authorization: Bearer <key>`. The relay holds no key: it asks
  AnythingLLM's `GET /api/v1/auth`, remembers a key it took for a minute (by its hash), and
  starts a run's `stream-chat` with the caller's key, which stays in that run's memory and
  never reaches the database or a log. A missing or refused key gets AnythingLLM's own
  answer, 403 `{"error": "No valid api key found."}`; an AnythingLLM that can't be reached
  is a 502.
- AnythingLLM's developer keys are all alike (each has the whole `/api/v1`), so any key
  sees and can cancel every run, as it could read every thread.
- `GET /everythingllm/health` is how a client tells the relay is there: its JSON names the
  service and its features. AnythingLLM answers a path it doesn't know with its web app's
  page and a 200, so look at the body, not the status.

Errors are `{"error": "..."}`. The routes, under `/everythingllm`:

| Route | Does |
| --- | --- |
| `POST /v1/runs` | `{"workspace", "thread", "clientId", "body"}` starts a run: 201 with the run. `body` is what the client would send `stream-chat` (a non-empty `message`, or `"reset": true` to clear the thread; `mode` and `attachments` as AnythingLLM takes them), forwarded as it came: without `mode` the workspace's own mode answers, `automatic` included. The body is held only in memory for the call, so attachments never reach the database, and no size limit is set (a 20 MB attachment goes through). A `clientId` already used answers 200 with that run and starts nothing; a thread with a running run answers 409. |
| `GET /v1/runs?status=running` | runs with that status (`running`, `done`, `failed`, `cancelled`), oldest first; every kept run without `status` |
| `GET /v1/runs/{id}` | the run (`id`, `clientId`, `workspace`, `thread`, `mode` (the body's, or null), `status`, `createdAt`, `finishedAt`); 404 when unknown or expired |
| `GET /v1/runs/{id}/events` | server-sent events: `chunk` with each chunk AnythingLLM sent, as it came and in order (an agent's `agentThought`s, the closing chunk and the `finalizeResponseStream` with its sources included), then one of `done` `{}`, `failed` `{"error"}` (for a non-2xx answer, an `error` or `abort` chunk, which isn't passed on, or a broken connection), `cancelled` `{}`, and the stream closes. Ids count from 1; `Last-Event-ID: n` starts after n. `: ping` every 15 s while live. Any number of followers. |
| `POST /v1/runs/{id}/cancel` | closes the upstream connection and ends the run `cancelled`; a run that has ended is left as it is |
| `GET /health` | `{"ok": true, "service": "everythingllm", "features": ["runs"]}`, no key |

Runs and their events are in SQLite (`~/.local/share/everythingllm/relay/relay.db`, mode 600),
written as each event arrives. A restart fails the runs it cut short with "The relay
restarted during the answer." and keeps their events; finished runs are deleted after 7
days (`RUN_RETENTION_DAYS`). The schema's version is SQLite's `user_version`; opening an
older database deletes its runs. With `NTFY_URL` set, a finished or failed run posts "Answer
ready" or "Answer failed" to that ntfy topic, with the question's first 120 characters
and `run=…,workspace=…,thread=…` as its tags; never the answer. A reset isn't notified.

Its settings live in `~/.config/everythingllm/relay.env` (mode 600), outside the repo, which the
AnythingLLM container mounts: only the optional `NTFY_URL` and `NTFY_TOKEN`, which are
secrets (research-runner reads them too; see "Deep research"). `uv run hostctl relay-setup` makes the file, builds the service image, maps
`/everythingllm` on :3001 and starts the container; `uv run hostctl relay-logs` follows it
(any app's `<app>-logs`). `relay.app`'s docstring lists the rest of the config. A client's
key never appears in a response or a log line, and a test holds that.

The relay runs in a service container (`host/quadlet/relay.container.in`, see "Service
containers"), with 512 MB and one CPU. It mounts the repo read-only, its venv folder
(`venvs/relay-ctr/`) and its database's folder (`relay/`), and nothing else: it has no
socket and nothing in storage. Its secrets come in as values podman reads from `relay.env`
on the host, not as a file. Its only way out is the egress proxy's `relay` profile:
AnythingLLM at `https://<PUBLIC_HOST>:3001` (`ANYTHINGLLM_URL`, since the container can't
reach the host's loopback), the ntfy host on :443 (`NTFY_HOST` in `host.env`, if it isn't
`ntfy.sh`), and PyPI for its first sync; nothing else, public or not. So the key check
and stream-chat go through the machine's :3001 route rather than straight to AnythingLLM.
It listens on `0.0.0.0:8446` inside (`RELAY_HOST`), published on the host's
`127.0.0.1:8446`, where the machine's route and the health check reach it. Through that port
every connection arrives from the container's own address (`10.89.79.10`), so that is the
one peer whose `X-Forwarded-For` and `X-Forwarded-Proto` uvicorn believes
(`FORWARDED_ALLOW_IPS`), and with loopback the only one it answers (`LocalPeers`):
another container on egress-net that reaches the port gets a 403, logged by its own
address.

Where it differs from the original spec: the relay adds nothing to the body and doesn't
interpret the answer (no `mode` default, no pieces or citations of its own); a
connection to AnythingLLM that breaks mid-answer, or ten silent minutes, fails the run
("The connection to AnythingLLM broke during the answer.") rather than completing it with
what came; and a `clientId` is remembered as long as its run is kept, so reusing it later
returns that old run.

## SearXNG

The agent's web search goes to a private SearXNG, a metasearch engine: each query fans
out to Google, Bing, Wikipedia and others and the merged results come back as JSON.
Nothing is indexed locally and no API keys are needed.

SearXNG is deployed by Ansible, not from this repo: the Quadlet unit
`searxng.container` and its config in `/srv/searxng/settings.yml` (JSON output on,
limiter off). It listens on 127.0.0.1:8888, and the machine routes HTTPS :8888
to it, since the AnythingLLM container can't reach the host's loopback.

AnythingLLM uses it as the search provider (Agent Skills > Web Search > SearXNG), with
base URL `https://<PUBLIC_HOST>:8888/search`. The same can be set through the
local API, logged in with the password (AnythingLLM's internal API needs it; see "AnythingLLM's
password" below):

    read -rsp 'AnythingLLM password: ' pw; echo
    token=$(curl -s localhost:3001/api/request-token -H 'Content-Type: application/json' \
      -d "$(jq -n --arg p "$pw" '{password: $p}')" | jq -r .token)
    curl -X POST localhost:3001/api/system/update-env -H "Authorization: Bearer $token" \
      -H 'Content-Type: application/json' -d '{"AgentSearXNGApiUrl":"https://<PUBLIC_HOST>:8888/search"}'
    curl -X POST localhost:3001/api/admin/system-preferences -H "Authorization: Bearer $token" \
      -H 'Content-Type: application/json' -d '{"agent_search_provider":"searxng-engine"}'

Some engines block servers now and then (DuckDuckGo answers 403 and is suspended for a
few minutes); the response's `unresponsive_engines` lists them. Check with

    curl -s 'http://127.0.0.1:8888/search?q=test&format=json' | jq '.results | length, .unresponsive_engines'
    journalctl --user -u searxng -f
