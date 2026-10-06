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
`host.env`, the tools the units run (podman, uv and zola at fixed paths), lingering,
tailscale, and the storage folder, creating the folders the containers mount inside it.
Then it does the following:

1. renders and starts the units (`uv run hostctl units`)
2. waits for AnythingLLM
3. deploys (`uv run hostctl deploy`)
4. points web search at SearXNG
5. runs every setup target: tailnet ports, sandbox, podcast
   timers, research, sites and audit runners
6. runs `uv run hostctl health`
7. ends with a checklist of what only AnythingLLM's UI can do. Each item is ticked when
   it's already done: the chat model and embedder, a DeepSeek key, the agent limits in the
   `.env`, SearXNG answering, a workspace, the built-in skills to turn off, Gmail.

Every step only changes what's out of date, so running it again is safe.

## Host config

This machine's settings live in `host.env` at the repo root, which git ignores. To set it
up, copy `host.env.example` and fill it in:

- `PUBLIC_HOST`: the tailnet name. The pages site (:8445) and SearXNG (:8888) are served
  on it through `tailscale serve`, and every link the setup hands out uses it.
- `ANYTHINGLLM_STORAGE`: AnythingLLM's storage directory on the host.

These read it:

- hostctl, which passes both on to what it runs
- `hostctl.sync`, for `ANYTHINGLLM_STORAGE`
- the host's systemd units, through `EnvironmentFile=@REPO@/host.env` (filled in by `uv run hostctl units`)
- the site builds, which read `PUBLIC_HOST` from it and pass zola
  `--base-url https://<PUBLIC_HOST>:8445/<site>`, so `zola.toml` doesn't name the host.
  They find the file at the root of the repo the sites are in, and the container sees it at
  `/mcp/host.env`, so builds that get none of our environment, like a script's
  `sites-write`, use it too.

Code running on the host derives its storage paths from `ANYTHINGLLM_STORAGE`. Inside the
container that variable isn't set, and storage is `/app/server/storage`. Tests ignore
`host.env`, so they run the same on any machine.

### Containers and tailnet ports

This repo owns the containers the setup runs, as templates in `host/quadlet/`; besides
the service containers (see "Service containers"), these two:

- `anythingllm.container`: AnythingLLM, pinned by digest, because the log filter depends on
  its internals
- `static_agent.container`: a Caddy container that mounts `host/caddy/pages.Caddyfile` from
  the repo, so its CSPs are versioned, and serves two sites:
  - **the pages site** (:8445): the Zola sites, `/podcasts` and the link cards, from
    `pages/public/`. `default-src 'self'; script-src 'none'`: no scripts, no inline styles,
    and nothing fetched from another host, so CSS can't send anything out either.
    `form-action 'none'; base-uri 'none'` cover what `default-src` doesn't: no form posts
    anywhere, and no `<base>` repoints a page's links. Its front page and the workspace
    pages' old addresses redirect to :8447.
  - **the workspace pages site** (:8447): every sandbox workspace's `/public`, mounted
    read-only from `sandbox/public/` and served as it is (see "Code sandbox"). The same
    policy, but inline CSS is allowed. It's a port, and so a browser origin, of its own, so
    that whatever its pages ever run can't read the podcasts' private feeds or post to
    `/news/write`. Scripts are off for every workspace; `@scripts` in the Caddyfile is the
    switch for letting one workspace's pages run them (`script-src 'self'
    'unsafe-inline'`, still nothing from other hosts), and matches nothing yet.

  `publish` and the sandbox's replies warn the agent when a page uses something the CSP
  blocks (scripts, stylesheets, fonts or images from other hosts), since the page would
  otherwise just render without it.

The host's own units in `host/systemd/` (services, timers, and the AnythingLLM drop-in) are
templates too. `uv run hostctl units` renders all of them:

- `host/quadlet/*.container.in` goes to `~/.config/containers/systemd/`
- `host/systemd/*.container.d/` goes next to it
- `host/systemd/*.service` and `*.timer` go to `~/.config/systemd/user/`

It fills in `@REPO@` (the checkout's path) and the `host.env` settings, and saves older
versions to `~/.local/share/everythingllm/backups/`. Then it reloads systemd and restarts what changed: a container
whose unit or drop-in changed, or a host unit that's running. A change to comments alone
restarts nothing. A guarded runner with a run going is left running, and a container whose
image of ours or network isn't there yet isn't started: its app's setup makes them (see
"Service containers"). Enabling a host unit is up to its app's `uv run hostctl <app>-setup` (see "The apps" below).

Run it from the main checkout. It refuses to run in a worktree, since the units run the
repo they were rendered from. Edit the templates, never the installed copies; `uv run hostctl diff`
shows where the two differ.

An Ansible playbook used to install the two containers' units and `/srv/static-agent-config/`.
It must leave them alone now, or its next run undoes `uv run hostctl units`.

### The apps

Every app this repo runs is declared once, in `packages/apps/src/apps/apps.toml`: its units
and the audit's label for each, its socket, its tailnet mappings, whether its restarts wait
for a run (the guard), its health checks, the steps its setup runs first, and whether
`uv run hostctl install` sets it up (and if not, why). hostctl and the audit read it through
`packages/apps` (standard library only, like `hostctl`);
app code never does. `uv run hostctl apps` lists the apps; for each:

- `uv run hostctl <app>-setup` runs its `before` steps (the sandbox's and the service
  containers' image builds, the agents, relay and gateway key files), maps its tailnet paths,
  enables and (re)starts its units and (re)starts its containers, asking first while a
  guarded one has a run going (`FORCE=1` doesn't ask), and starts its timers
  (`hostctl.appctl`).
- `uv run hostctl <app>-logs` follows its units and the ones it watches.
- `uv run hostctl serve-setup` maps every app's tailnet paths that aren't mapped yet with
  `sudo tailscale serve`, and leaves other mappings on the machine alone.
- `uv run hostctl health` checks every app's units, health URLs and sockets.

Adding an app: its code, its unit template in `host/`, and one entry in `apps.toml`.
`packages/apps/tests/test_apps.py` says what's missing: a template no app owns, a unit
without a template, two mappings on one port, or a port that isn't the one the code or the
unit uses.

### AnythingLLM's password

AnythingLLM's own API (`/api/...`, which its UI uses; not the developer API's `/api/v1/`) answers
anyone who reaches it until it has a password, and tailnet :3001 reaches it. That includes
scheduled jobs, which run the agent with every tool approved, `.env` changes and new API keys.
So it gets a password (Settings > Security > Password protection; long and random, from
`[a-zA-Z0-9_-!@$%^&*();]`), which AnythingLLM keeps in plain text as `AUTH_TOKEN` in storage's
`.env`, beside a `JWT_SECRET` it makes.

Our callers of that API log in with it: `hostctl.sync` and `hostctl.machine` through
`units.anythingllm_headers`, the audit and research's workspace embedding through
`hostrpc.anythingllm_headers`. Each logs in once per process (a login lasts 30 days and is
logged) and once more after a 401; with no password set they send nothing. The relay uses the
developer API key and doesn't log in. `uv run hostctl health` and the audit's `security` check fail when
`/api/scheduled-jobs` answers without a login.

What a password doesn't close: `/api/request-token` has no rate limit, so the password has to
be long; a developer API key (the relay's) still has full `/api/v1` access, `update-env`
included; the agent's websocket needs only an invocation's id.

Not in this repo, so a new machine needs them first: rootless podman with Quadlet, systemd
lingering for the user, tailscale, uv, zola in `/usr/local/bin`, SearXNG (deployed by Ansible,
see SearXNG below) and Ollama if it's the embedding provider. AnythingLLM's own settings
(providers and keys in its `.env`, workspaces, which built-in skills are off) are set
through its UI.

## Layout

- `anythingllm/agent-skills/<hubId>/` — custom agent skills (`plugin.json` + `handler.js`)
  - `deep-research/` — multi-source web research with GLM and DeepSeek, published to the
    `research` site; hands the work to `research-runner` on the host (see "Deep research")
  - `run-code/`, `write-file/`, `publish/`, `build-site/` — the code sandbox, run by
    `sandbox-runner` on the host (see "Code sandbox")
  - `write-entry/`, `delete-entry/`, `add-podcast/`, `remove-podcast/`, `publish-report/`,
    `run-job/` — the ops of the sites, podcasts and audit runners that write or act. They're
    skills, not MCP tools, so they can refuse a delegated task (below); each forwards one op
    to its runner (`forwardSkill` in `_lib/runner.js`). They're generated: each is declared
    in its front's `server.py` like a tool, a signature with a docstring and no body, under
    `@skills.add` (`hostrpc.Skills`), and `uv run hostctl skills` writes its `plugin.json` and
    `handler.js` from that (`hostrpc.skillgen`), so edit the declaration, not those files.
    `uv run hostctl diff` and `uv run hostctl deploy` stop when they're stale. A param the agent leaves out is
    left out of the op's args, so the op's own default applies; a test holds a declaration's
    parameters and defaults to its op's.
  - `_lib/` — what the skills share (no `plugin.json`, so AnythingLLM doesn't load it as
    a skill): `hostrpc.js`, the node side of `packages/hostrpc`; `sandbox.js`; `runner.js`;
    and `delegated.js`, the check that makes every skill of ours that writes, acts or
    delegates refuse a call from an `agents-*` workspace, where delegated tasks will run
    (`docs/.proposals/agents.md`). A test holds every skill to it; elsewhere, chats, the
    Nilson relay's API chats and scheduled jobs, nothing changes.
- `anythingllm/mcp_servers.json` — deployed to `storage/plugins/anythingllm_mcp_servers.json`
- `anythingllm/env.example` — keys used in the live `.env` (values stay out of git)
- `anythingllm/system-prompt.md` — the system prompt for chat and the agent: which tool
  to reach for, the tool-call budget, safety rules. Deployed through the API to every
  workspace and as the default for new ones. Scheduled jobs have no workspace, so they get
  AnythingLLM's built-in prompt instead; their own prompts carry what they need.
- `anythingllm/scheduled-jobs/<slug>/` — scheduled jobs (`job.json` with name, cron and
  tools, plus `prompt.md`), deployed through the AnythingLLM API and matched by name
  - `daily-news-page/` — writes the day's Daily News edition (US, Sweden, World) to the
    `news` site from the feed headlines of the `sites` server's `headlines` tool; cron is UTC inside the container (18:00 UTC = 20:00 Stockholm in summer, 19:00 in
    winter), and the prompt dates the edition by Stockholm time
  - `system-audit/` — runs the audit checks and publishes the day's report to the `status`
    site at 16:00 UTC (18:00 Stockholm in summer, 17:00 in winter), dated by Stockholm
    time (see below)
- `anythingllm/slash-commands/<name>/` — slash command presets: `/<name>` (a-z, 0-9,
  `_`, `-`), `command.json` with its description, and `prompt.md`; deployed through the
  AnythingLLM API and matched by command. Typing the command in chat swaps in the prompt, and whatever
  follows it stays after the prompt; a prompt that starts with `@agent` runs in agent mode.
  - `deep-research/` — `/deep-research <question>` runs the deep-research skill on it
- `packages/` — MCP servers we write: members of the uv workspace at the repo root
  (`pyproject.toml`, `uv.lock`), one per subdirectory
  - `packages/sites/` — the Zola sites on the tailnet pages site (:8445): list/write/get/delete
    their entries and build them; and `headlines(section)`, the last 30 hours' stories for
    the Daily News job from the feeds in `FEEDS` (`sites/feeds.py`), each with its own link.
    The MCP server forwards to `sites-runner` on the host, which does the work. The sites'
    sources are in `packages/sites/zola/` (see below)
  - `packages/audit/` — health checks over this setup, for the System Audit job (see below); the
    MCP server forwards to `audit-runner` on the host, which runs them
  - `packages/sandbox/` — not an MCP server: `sandbox-runner` runs the agent's Python and bash
    in throwaway podman containers on the host, with only PyPI on the network, and
    publishes pages from them, for the `run-code`, `write-file`, `publish` and `build-site` skills (see
    "Code sandbox" below)
  - `packages/podcasts/` — downloads podcast episodes, finds their ads, and serves them without
    those as private feeds on the pages site (see "Podcasts" below); the MCP server forwards
    to `podcasts-runner` on the host, which does the work. Its audio code is here too:
    `podcasts.avio` (PyAV decoding, and cutting without re-encoding), `podcasts.fingerprint`
    (finds the stretches recordings share), `podcasts.whisper` (speech to text, with the
    transcripts' types in `podcasts.segments`). `podcasts.cli` runs them by hand as `spot
    repeats` and `transcribe`, with the models in `~/.local/share/everythingllm/podcasts/models` as the
    services use them
  - `packages/hostrpc/` — a library, not a server: how the MCP servers and skills talk to the
    services on the host (see "Services on the host" below)
  - `packages/splice/` — not an MCP server: `splice-web` serves the podcasts, putting each episode
    together from the untouched download and the stretches to leave out (see "Originals,
    cuts and podcasts-web" below)
  - `packages/research/` — not an MCP server: `research-runner` runs the deep-research skill's
    runs on the host, and `research-run` runs one by hand (see "Deep research")
  - `packages/agents/` — not an MCP server: `agents-runner` runs delegations, tasks done by
    AnythingLLM's own agents, and `agents-run` starts one by hand (see "Delegation")
  - `packages/runs/` — a library, not a server: what research-runner and agents-runner share
    for long runs: run state with long-poll waiting and slots, the run log, live cards
  - `packages/publicweb/` — a library, not a server: the HTTP client podcasts, sites and research use,
    which refuses LAN, tailnet and loopback hosts, and `publicweb.pages`, the page reader on
    it that the article writer and research share
  - `packages/chatimage/` — a library, not a server: the pictures the host draws for the chat,
    which the agent shows as Markdown images: link cards for published pages (see "Code
    sandbox"), deep research's live progress cards, and the server push that keeps a live one
    current (see "Deep research")
- `packages/egress/` — the egress proxy, the service containers' only way out, and
  `egress.toml`, their addresses and what each may reach (see "Service containers")
- `packages/relay/` — the Nilson relay, a host service for the Nilson chat app rather than for
  AnythingLLM's agent; also a workspace member (see "Nilson relay")
- `packages/gateway/` — the MCP gateway, a host service that serves the fronts' tools over
  HTTP to MCP clients other than AnythingLLM (see "MCP gateway")
- `host/systemd/` — host user units, rendered into `~/.config/systemd/user/` (`uv run hostctl units`);
  each one's `Description=` says what it does, and its app's `uv run hostctl <app>-setup` (see "The
  apps") enables it.
  `anythingllm.container.d/` is a Quadlet drop-in that preloads `anythingllm/log-filter.js`
  to cut MCP payloads from AnythingLLM's log.
- What only host services read or write lives in `~/.local/share/everythingllm`
  (`hostrpc.data_dir()`), not in AnythingLLM's storage, which the container mounts. It's
  laid out by kind:

      venvs/<name>/        the host services' venvs (agents, audit, gateway, podcasts,
                           relay, research, sandbox, sites, splice)
      venvs/<x>-ctr/       a service container's venv and uv cache (venv/, uv-cache/)
      pages/public/        the pages site Caddy serves
      pages/entries/       the Zola entries
      sandbox/workspaces/  the sandbox's folders, one per workspace (threads/, project/,
                           shared/)
      podcasts/            the podcasts' state, audio, transcripts and manifests
      podcasts/models/     Whisper's
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
- `host/containers/` — the images we build: the sandbox's (`uv run hostctl sandbox-images`) and
  the service containers' (`uv run hostctl service-images`)
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
  - `appctl` — the apps' setup, logs and tailnet mappings, from the registry
  - `run_guard` — asks before a runner with a live run restarts
  - `agents_env`, `relay_env`, `gateway_env` — the agents, relay and gateway setups' key
    file checks; `gateway_env` also adds a gateway client (`uv run hostctl gateway-client`)
  - `skills` — writes the generated skills (`uv run hostctl skills`)
  - `health.sh` — `uv run hostctl health`

## Workflow

`uv run hostctl` lists every command. Day to day: `uv run hostctl diff` shows what would change live,
`uv run hostctl deploy` copies it into storage (old files go to
`~/.local/share/everythingllm/backups/`), refreshes the MCP deps
and restarts AnythingLLM, `uv run hostctl test` runs every test and `uv run hostctl health` checks every unit,
port, host service and MCP server. `uv run hostctl import-skill <hubId>` (and `import-job`,
`import-command`) brings something made in the UI under the repo.

Skill handlers are re-required on each load, so skill changes don't need a restart, but
`uv run hostctl deploy` also runs `uv run hostctl mcp-sync` and `uv run hostctl restart`, so AnythingLLM and every MCP
server it starts run the code and deps that were just deployed.
On deploy, a skill's `active` flag and any setup_args `value` saved through the UI
are kept from the live `plugin.json` unless the repo sets a `value` itself.
Scheduled jobs keep their live enabled toggle; deploying one reschedules it right away.
Deploy doesn't delete slash commands that exist only live; remove those in the UI.

## uv cheatsheet

The repo root is a uv workspace; each `packages/<name>/` is a member with its own dependencies
and console scripts, all locked together in `uv.lock`. Run these from the repo root; the dev
venv is `.venv` there, which is the interpreter `.vscode/settings.json` points at.

    uv sync --all-packages                     # install every member + dev deps into .venv
    uv run --all-packages --all-extras pytest -q   # all tests (what `uv run hostctl test` runs)
    uv run --package podcasts --extra host pytest packages/podcasts -q   # one member's tests

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
  what the front imports, and puts the rest in a `host` extra (`podcasts`, `sites`); its
  units run with `--extra host`. AnythingLLM starts each front with `uv run --package`,
  which installs that member's base dependencies, so Whisper and PyAV stay out of
  the container.
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
`{{`, `{%` and `{#` get a zero-width space between the characters, and zola runs with only
`PATH` in its environment, so nothing an entry says can read secrets or files. It also
runs without a network, in a user and network namespace of its own (`unshare`), so a
template's `load_data` can't fetch anything, not even from the host's loopback, and a build
is stopped after 40 s. A machine without unprivileged user namespaces builds without the
namespace and logs a warning. The
front matter names the slug, so a file like `2026-10-01-notes.md` keeps its date in the URL. The build (`sites.build`, also the `sites-build`
command) assembles the site from the repo plus its entries in a temp dir, builds it next
to `~/.local/share/everythingllm/pages/public/<name>/` and swaps it in, holding a lock on `.build.lock` in the entries folder. Entries live outside storage because
only host services read or write them; the AnythingLLM container never needs them.
Built sites carry a `.zola-site` marker; the build won't replace a directory without one,
and the sandbox won't publish over a directory that isn't its own page.

**Built in the sandbox.** A site whose repo `zola.toml` names its theme's origin,
`[extra.build] theme_from = "system"` (the repo's `packages/sites/zola/themes`) or a sandbox workspace's
name (its `/shared/<name>/themes/<theme>`), isn't built by the host's zola. `sites.build`
asks `sandbox-runner` (`build_system_site`), which builds it in a container with no
network: the site's repo source and its entries mounted read-only, the theme put in place
by the repo's `sitebuild.py`, and the output copied (plain files only) into
`pages/public/.<name>.new`, which `sites.build` marks and swaps in as before. News,
research and status are all built that way, with `theme_from = "system"`, so they look and
build exactly as they did; pointing one at a workspace's theme is a one-line change to its
`zola.toml`, after which that workspace's theme edits restyle the site at its next build.
Their entries stay on the host and are written exactly as below; no host service reads or
writes a sandbox folder. The operation takes only a site's name and reads the rest from
the repo, since its socket is reachable from the AnythingLLM container. With
`sandbox-runner` down, those sites can't build, so their writes fail and are undone.

The sites MCP server's builds are started on the host, in `sites-runner`, and the audit's
report in `audit-runner`, as every other writer's are; nothing in the container builds. Other writers use the
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
(`compile_sass = false`). The status site puts each report's findings in its title
("System audit — October 3, 2026 — 1 failing, 2 warnings"), which shows in the browser tab
and the feed whatever the stylesheet does.

The sites:

- `news` — the Daily News: the home page (`/news/`) always shows the newest edition,
  each edition stays at `/news/editions/YYYY-MM-DD/`, and `/news/editions/` is the archive.
  Headlines open articles the bot writes on the first click, kept at
  `/news/articles/<desk>-<n>-<day>/` (see "News articles").
- `research` — reports from the deep-research skill at `/research/reports/<title-slug>/`.
- `status` — the System Audit's daily report: the home page (`/status/`) shows the newest,
  each stays at `/status/reports/YYYY-MM-DD/`.

A section can set two things under `[extra]` in its `content/<section>/_index.md`:
- `agent_readonly = true`: the `sites` server (and `sites-write`) can read the section
  but refuses to write or delete there. The news `articles` are only written by the article writer,
  the status `reports` only by the audit server.
- `[extra.audit]`, checks for the audit: `max_age_days` (warn when the section's newest
  entry is older), and `required`, paths every entry's fields must have, e.g.
  `"sections[].stories[].url"` for the news `editions`.

A new site: add `packages/sites/zola/sites/<name>/` with `theme = "agent-site"`, its sections and an
`agent_help`, run `uv run hostctl deploy`, and point a job or chat at the `sites` server.

## MCP servers in AnythingLLM

The repo is mounted read-only into the AnythingLLM container at `/mcp` (see the
`Volume=` line in `host/quadlet/anythingllm.container.in`), and `mcp_servers.json` launches each server
with `uv run --frozen --project /mcp --package <name>`. The container's venv and uv
cache live in `/srv/anythingllm/storage/everythingllm/mcp/`. The container can't reach the host's
loopback, so servers run inside it over stdio rather than as host HTTP services. Other MCP
clients get the same tools over HTTP from the gateway (see "MCP gateway").

### Services on the host

Work that is heavy, long or needs the host goes to a service on the host instead, with the
MCP server or skill in the container as a thin front: `sandbox-runner` (the code sandbox),
`research-runner` (deep research), `agents-runner` (delegation), `podcasts-runner` (the podcasts tools),
`sites-runner` (the sites tools and their builds) and `audit-runner` (the audit's
checks). Each listens
on a Unix socket in storage, `storage/everythingllm/<name>/runner.sock` (mode 0660), which the container
sees without a Quadlet change, and they all speak `hostrpc`'s protocol: one request per
connection, a line of JSON each way, `{"op", "args"}` in and `{"ok": true, "result"}` or
`{"ok": false, "error"}` out.

- `hostrpc.Service(ops, errors=…)` dispatches each request to the function of that name in
  `ops` (a package's `tools.OPS`) or to an `op_<name>` method of a subclass (research,
  sandbox), running one that isn't a coroutine in a thread; a `hostrpc.RunnerError` becomes
  the error the caller sees, as does that of the service's own `errors` (sites-runner's
  `SiteError`); anything else is logged and reported as `runner error: …`. Every service answers `ping`, which
  `uv run hostctl health` and the audit ask. `hostrpc.serve` serves one on its socket and removes the
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
  `tools.py` (`packages/podcasts` is the example), a `<name>-runner` console script, a unit
  `host/systemd/<name>-runner.service` with its own venv in `~/.local/share/everythingllm/`, and
  an app `<name>` in `apps.toml` with `runner` naming that unit (see "The apps"). Its socket
  is `storage/everythingllm/<name>/runner.sock`, where `hostrpc.caller` looks. A runner may
  be a container instead (see "Service containers"): its `runner` is then the container's
  `<x>.service`.

Code edits go live the next time AnythingLLM starts the server (restart it from the
Agent Skills > MCP Servers page, `uv run hostctl restart`, or `uv run hostctl deploy`, which restarts). Note
that this runs whatever is in the working tree, committed or not. Requires `mcp` 2.x (`MCPServer`, not `FastMCP`).

`tailscale serve` maps tailnet HTTPS :8445 to the pages site and :8447 to the workspace pages
site.

### Service containers

A host service can run in a container of its own instead of as a host unit, hardened like
the sandbox's containers and with one way out, the egress proxy. Each service moves over
on its own: its template goes from `host/systemd/<x>.service` to
`host/quadlet/<x>.container.in`, and its app's `runner` and journal key follow (`apps.toml`'s
`container`, `systemd-<x>`).

**The image.** Every service container runs `localhost/everythingllm-service`
(`host/containers/service/Containerfile`): `python:3.12-slim`, the host's uv copied from its
own image, tzdata, the DejaVu and Liberation fonts chatimage draws with, and CA
certificates. It holds none of our code. `uv run hostctl service-images` builds it and
creates `egress-net`; the `egress` app's setup runs it first.

**The paths are the host's.** The repo is mounted read-only at its own path (`@REPO@`), and
the container runs `uv run --frozen --no-dev --project @REPO@ --package <pkg> [--extra host]
<script>` with `HOME=%h`. Everything else it mounts (its folders in the data dir and in
storage, its socket folder, the sandbox's socket) is mounted at its host path too, so a path
means the same inside and out: what the sandbox's `build_system_site` hands back, what a
runner tells the container, what lands in a run log. `host.env` comes in through
`EnvironmentFile=`. Each container has one folder of its own,
`~/.local/share/everythingllm/venvs/<x>-ctr/`, with its venv (`UV_PROJECT_ENVIRONMENT=…/venv`)
and its uv cache (`UV_CACHE_DIR=…/uv-cache`) in it: one mount, so uv can hardlink, and no
container can touch another's packages. The first start syncs the venv from PyPI through
the proxy (a minute or three); later ones find it synced.

**A code change reaches a container by a restart**, as it does a host unit: `uv run hostctl
<app>-setup`, or `systemctl --user restart <x>.service`. `<app>-setup` restarts an app's
containers (it doesn't enable them: Quadlet's `[Install]` does), asking first while a
guarded runner has a run going, as for a host unit. `uv run hostctl units` starts a changed
container, except a guarded one with a run going, and one whose image or network isn't
there yet, which waits for its app's setup.

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
    Environment=HTTPS_PROXY=http://10.89.79.2:3128   # and HTTP_PROXY, EGRESS_PROXY
    PublishPort=127.0.0.1:<port>:<port>              # one with an HTTP port

Quadlet in podman 5.4 has no `Memory=` or `Umask=`; `PodmanArgs` carries them, and
`hostctl`'s tests convert every template with `/usr/libexec/podman/quadlet -dryrun`, which
refuses a key it doesn't know. A template never sets `ContainerName=`: Quadlet's
`systemd-<x>` is the name the audit and `<app>-logs` find its journal by. Inside, the
`anythingllm` group shows as `nogroup` (65534): access through it works, but code can't
chgrp to it or look it up by name; storage's setgid folders give new files the group
anyway.

**Ports and addresses.** A service's HTTP port is published on the host's `127.0.0.1`, so
`apps.toml`'s `serve` and health checks are unchanged. What comes through arrives from the
container's own address, not its loopback, so a server in a container listens on `0.0.0.0`:
`LIVE_HOST` (the live cards, `runs.live`) and `ARTICLES_HOST` (the article writer) say so
in its template, and default to `127.0.0.1` on the host. Listening on `0.0.0.0` also lets
every other container on egress-net reach that port. Each of those ports is already on the
tailnet through `tailscale serve`, so a container gets no more than any tailnet device
does; this is accepted rather than split into a network per service. A container can't reach the
host's loopback either, so it reaches AnythingLLM and SearXNG by their tailnet names
through the proxy: `ANYTHINGLLM_API=https://<PUBLIC_HOST>:3001/api` (research) and
`SEARXNG_URL=https://<PUBLIC_HOST>:8888/search` (research, the article writer). Both default
to the host's loopback.

**The egress proxy** (`packages/egress`, the `egress` app) is egress-net's only way out.
`egress-net` is an internal podman network (`10.89.79.0/24`; `sandbox-net` is
`10.89.77.0/24`), with no route and no DNS. `egress-proxy` runs in a container of the same
image, on egress-net at `10.89.79.2` and on podman's default network for its own way out,
and listens at `10.89.79.2:3128`. It takes `CONNECT host:port` (https) and absolute-form
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
  | research | research-runner (.11)                                       | yes    | `PUBLIC_HOST:3001`, `:8888`   |
  | sites    | sites-runner (.12)                                          | yes    | `PUBLIC_HOST:8888`            |
  | podcasts | podcasts-runner, -sync-worker, -transcribe-worker (.13–.15) | yes    | —                             |

- A public host must resolve to public addresses only, all of them: the rule is
  `publicweb.public_address`, the one the services use on the host, so loopback, the LAN,
  link-local, the tailnet's CGNAT range and IPv4-mapped forms of them are all refused. The
  proxy resolves each name once and connects to the address it checked, so a name that
  answers differently the second time (DNS rebinding) gets nowhere.
- Anything else is refused with a 403 that says why, as is a connection from an address no
  profile has. Each connection logs its profile, method, `host:port` and verdict, never a
  path or a query (`uv run hostctl egress-logs`).

In a container, `EGRESS_PROXY` puts `publicweb.public_client` in proxy mode: every request
goes to the proxy, which makes the address check, and the client checks only the scheme.
Other clients (httpx, uv) follow `HTTPS_PROXY` and `HTTP_PROXY`. On the host none of these
is set, and nothing changes.

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
pages they changed, with their URLs and what in them the CSP blocks. Caddy's directory
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
token, and the thread `gateway` (see "MCP gateway"). Each run mounts:

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

They live in `~/.local/share/everythingllm/sandbox/workspaces/<workspace>/` (`threads/<thread>/`,
`project/` and `shared/`), out of the container's reach. The runner's own file operations
(`write-file`, `publish`) only take paths in the caller's own folders, never another
workspace's. A workspace's folders together are held to 5 GB: over that, runs and writes are
refused until the agent deletes something with `write-file`, and the refusal names the
biggest files and folders, since no run can look for them. A run warns past 4 GB. Runs in
one workspace take turns, since they share `/project`; while one is going, a write or
publish from any of the workspace's chats fails at once rather than waiting. Runs in
different workspaces overlap. `docs/.proposals/shared-sites.md` (kept out of git) has the design.

**The lab site** is the one site the agent controls entirely: templates, stylesheets,
`zola.toml` and content, in education's `/shared/education/sites/lab/`, where other
workspaces can read it and copy it. It started as a copy of the `agent-site` theme and a
welcome entry, with a `README.md` for the agent and a git repository so it can roll back.
It's built with `build-site` (`path` `/shared/education/sites/lab`, slug `lab`), which
puts it at `https://<PUBLIC_HOST>:8447/education/lab/`. Nothing in the repo or on the host reads it,
so it can break without breaking anything else, and the CSP still holds for whatever it
serves.

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
  `themes/`;
- it runs `zola build` with the base URL the runner passes in
  (`…:8447/<workspace>/<slug>`), so a site can't point its links at another host, within 60 s.

The runner copies the output into `/public/<slug>` (plain files only, in place of what was
there), so a site is live like any page; zola's error comes back if it doesn't build, and
nothing changes then. A build waits like a run (`op_wait`).

**Each run** gets a fresh `localhost/everythingllm-sandbox` container, with the script mounted
read-only from a host-only folder at `/sandbox`:

- non-root (`--userns keep-id`), read-only root, `--cap-drop ALL`, `no-new-privileges`;
- 1 CPU, 1 GB memory, 256 processes, 60 s by default (300 s max), then killed; a run
  that hits the memory limit is reported as such (podman's `OOMKilled`);
- output clipped to the first and last part; at most 2 runs at once;
- containers carry the label `everythingllm-sandbox=1`; the runner removes any left over
  from a crash or restart when it starts.

The host never follows a symlink out of a mount when it reads, writes or copies for the
agent, won't write into a FIFO or device there, and leaves symlinks out of what `publish`
or a build copies into `/public`; the sandbox can create any symlink it likes in its own
folders.

**Network.** Sandboxes sit on `sandbox-net`, a podman network made with `--internal`
(no route out) and `--disable-dns` (no DNS, so nothing leaks out through lookups either). Their
only way out is `sandbox-proxy` (tinyproxy, `host/systemd/sandbox-proxy.service`), which is
also on the default network and lets through only the hosts in
`host/containers/sandbox/allowlist`: `pypi.org` and `files.pythonhosted.org`. So `pip
install` works, and the internet, the LAN, the tailnet (AnythingLLM's API, Ollama, …)
and the host's own ports don't. `upload.pypi.org` stays blocked, so code can't push
data out through a package upload either. To allow another host, add an anchored regex to
`allowlist` and run `uv run hostctl sandbox-setup`, which rebuilds the proxy image.

The `logs` and `services` audit checks cover both units and ping the runner.

## Podcasts

The `podcasts` MCP server and the `add-podcast` and `remove-podcast` skills keep private
copies of podcasts: `add-podcast(url, keep)` subscribes to a show's RSS feed, and its newest `keep` episodes (default 5, up to 100, or
`"all"` for the whole catalog) are downloaded to `~/.local/share/everythingllm/podcasts/audio/` and listed in a
feed of our own, `https://<PUBLIC_HOST>:8445/podcasts/<slug>/feed.xml`, which podcasts-web
serves (range requests included, so players can seek). `/podcasts/` lists every feed. Ask the
agent, e.g. `@agent download the last 10 episodes of Hard Fork`, then paste the feed URL
into a podcast app. The MCP tools: `find_podcast`, `list_podcasts` (downloads, progress,
errors), `search_podcasts`, `refresh_podcasts`; `remove-podcast` deletes the downloads.

`find_podcast(query)` turns a show's name, an Apple Podcasts link, the show's website or a
feed URL into feed URLs for `add-podcast`. Names go to Apple's podcast directory (the iTunes
Search API, no key), Apple links are looked up by their id, and web pages are read for their
`<link rel="alternate" type="application/rss+xml">`. Every candidate is fetched and parsed
first, and the reply lists each working feed with its title, author, episode count and
latest episode; the whole call, checks included, has 45 s (as does `add-podcast`'s fetch),
inside AnythingLLM's 60 s tool limit. A page with no feed link (Spotify,
Amazon Music, Audible and iHeart pages never have one) gets a note to search by name, since
a show found only in such an app has no public feed.

- Use an app that fetches feeds from the phone itself (AntennaPod, Podcast Addict), with
  Tailscale on. Apps that fetch through their own servers (Pocket Casts, Overcast, Apple
  Podcasts' sync) can't reach a tailnet address.
- The MCP server in the container only forwards each tool call to `podcasts-runner` on the
  host (`packages/podcasts/src/podcasts/tools.py`, `host/systemd/podcasts-runner.service`, its
  own venv in `~/.local/share/everythingllm/venvs/podcasts`, socket `storage/everythingllm/podcasts/runner.sock` (the rest of its data is in `~/.local/share/everythingllm/podcasts/`);
  see "Services on the host"), which runs the tool and sends back its text.
  The feeds, the model's key and the audio stack never touch the container: the sync
  and transcription run on the host too, from the same venv.
- MCP tool calls time out after 60 s, so the runner only starts the sync and returns. A
  sync is a unit of its own, `podcasts-sync@<slug>.service`, or `podcasts-sync@_all.service`
  for every feed (what the timer starts), so restarting the runner, AnythingLLM or the
  container stops none. A sync that dies within a second is reported as a tool error with
  the last line of its log. The sync takes `~/.local/share/everythingllm/podcasts/sync.lock` and exits at once if
  another holds it, and it takes
  one feed and one episode at a time, rewrites `feed.xml` after every download, deletes
  episodes that fall out of the newest `keep`, and picks up feeds added while it runs. Its
  output goes to `~/.local/share/everythingllm/podcasts/sync.log`; what each show has is in
  `~/.local/share/everythingllm/podcasts/shows/<slug>.json`, subscriptions in `~/.local/share/everythingllm/podcasts/feeds.json`, and
  when the last sync started and finished (and its traceback, if it crashed) in
  `~/.local/share/everythingllm/podcasts/last_sync.json`, which `list_podcasts` reports on.
- A feed that fails to load, or crashes the sync, gets the error in its record and keeps
  its downloads; the other feeds still sync. A feed that comes back with no episodes keeps
  what it has too ("the feed lists no episodes right now"), rather than being pruned to
  nothing. Downloads pause for the rest of a sync when the disk has under 20 GB free.
- New episodes are downloaded newest first, at most 30 a day per show (`DAILY_DOWNLOADS`,
  counted in `~/.local/share/everythingllm/podcasts/downloads.json` by the day in `PODCASTS_TZ`). A show's
  regular episodes never come near that; a catalog (`keep="all"`) comes down over days
  instead of filling one sync for hours, and the other shows' new episodes still get
  through. Its record says how many more wait.
- `add-podcast(rules="...")` says in plain words which episodes to download: "skip the
  spin-off It Could Happen Here", "skip weekend episodes", "only the nightly episodes Jon
  Stewart hosts; skip compilations, recaps and archive episodes". AnythingLLM's default
  model (DeepSeek) reads each episode's title, description, length, and weekday and date
  in `PODCASTS_TZ` (default Europe/Stockholm; worked out by the code, since models get
  weekdays wrong) and answers keep or skip with a reason (`rules.py`), 20 episodes a
  request and only as far back as `keep` needs. Skipped episodes aren't downloaded and
  don't count toward `keep`, and ones already downloaded are deleted at the next sync;
  `list_podcasts` lists the skipped ones with the model's reasons, and `sync.log` every
  verdict. `rules=""` downloads everything.
  - Each verdict is asked for once and kept in `~/.local/share/everythingllm/podcasts/verdicts/<slug>.json`
    until the rules change, since a model asked twice may answer differently, and an
    episode that flipped to skipped would be deleted.
  - When the model can't answer (no DeepSeek key, an error, an answer that doesn't cover
    every episode), the feed's record says so, downloaded episodes stay, and new ones wait
    for the next sync, along with anything older: an old episode fetched in the meantime
    would only be pruned once the newer one is judged.
- `podcasts-sync.timer` runs the sync every 6 hours (`uv run hostctl podcasts-setup`; it replaced
  a scheduled job that only called `refresh_podcasts`, so no agent is involved). A sync
  killed midway (a reboot, or `uv run hostctl units` changing its unit while it runs) leaves its
  episode for the next one to download again, and a day later that cleans up what the killed
  one left.
- Our feed is built from scratch from the show's title, art and episode details, not
  copied, so `itunes:new-feed-url` and the like can't send the app back to the public feed.
- When a show moves its feed, the sync follows: after a permanent redirect (301/308), or to
  the feed's `itunes:new-feed-url` if that feed loads and has episodes (one move per sync,
  so feeds pointing at each other can't loop), it saves the new URL in `feeds.json` and
  logs the move to `sync.log`. Our private feed URL stays the same.
- Feed and episode URLs must be http(s) and resolve to public addresses (checked on every
  redirect too), so a feed can't make the server fetch the LAN, the tailnet or AnythingLLM's
  API onto the pages site. Only known audio and video types are kept (a `text/*` reply is
  refused), at most 2 GB per episode.

### Originals, cuts and podcasts-web

A downloaded episode is never changed. What to leave out of it is a list beside it, and what
an app downloads is put together from the two each time it's fetched, so a cut can be
changed or undone, and nothing is stored twice.

- `~/.local/share/everythingllm/podcasts/audio/<sha256>.<ext>`: each episode as downloaded, named by its hash.
  The fingerprints (`prints/<slug>/<sha256>.npy`) and transcripts
  (`transcripts/<slug>/<sha256>.json`, in the original's times) are keyed by the same
  hash, so they stay right whatever is cut.
- `~/.local/share/everythingllm/podcasts/cuts/<sha256>.json`, the sidecar: `{audio, cuts: [{start, end, source,
  reason, active}], legacy_cut}`, in seconds of the original. `source` is `repeat` (the ad
  scrubber), `ad-read` (the transcript's ad reads) or `agent`. Inactive cuts stay in the
  list but aren't left out; that's how `ad_words="report"` keeps its reads.
- `~/.local/share/everythingllm/podcasts/manifests/<slug>/<name>.json`: what podcasts-web serves at
  `/podcasts/<slug>/<name>`. For an MP3, it's byte ranges of the original with the frames
  (26 ms each) that start inside a cut left out, plus a new Info/Xing frame with the new
  frame count and seek table, so apps show the right length and seek right. Nothing is
  re-encoded, and with no cuts it's the original byte for byte. Chapters in the ID3 tag
  are dropped when there are cuts, since their times would be wrong. Anything that isn't an
  MP3, or an MP3 `splice` can't read, is cut once with PyAV into
  `audio/<sha256>.<cutid>.<ext>` instead.
- The served name is `<stem>.<ext>` with no cuts and `<stem>.<cutid>.<ext>` with some,
  `cutid` being a hash of the active cuts. So a podcast app sees a new file whenever the
  cuts change, and the `.vtt` beside it is renamed and shifted to match. Whatever changes a
  sidecar calls `Library.render`. The sync also renders every episode, so a sidecar edited
  by hand takes effect at the next sync. A replaced manifest is kept for a day, for apps
  that fetched the feed before the change.
- At the end of each sync, originals, sidecars and manifests that no feed's record refers
  to are deleted. Originals wait a day, in case a download isn't in a record yet.
  `remove-podcast` deletes the show's at once.
- `splice-web` (`packages/splice`, standard library only, `host/systemd/podcasts-web.service`,
  its own venv in `~/.local/share/everythingllm/venvs/splice`) is mapped to `:8445/podcasts` by
  `tailscale serve`, ahead of the pages site's Caddy. It serves manifests with range
  requests, `HEAD`, `ETag`/`If-Range` and `sendfile`. Anything else under
  `~/.local/share/everythingllm/pages/public/podcasts/` (feeds, transcripts, the index) it serves as a file, with the
  pages site's CSP and `nosniff`, never following a symlink or leaving that folder.
  `uv run hostctl health` checks it, and the audit reads its journal.
- Episodes downloaded before this kept only their cut file. The first sync after the change
  moves each into `audio/` as its original, with the time already cut noted as `legacy_cut`,
  and keeps its served name, so podcast apps see no change.

### Ad scrubbing

Each sync finds the ads in new episodes before they join the private feed. Ads, and the
bumpers around ad breaks, play in many episodes of a show and the talk in only one, so the
sync leaves out every stretch of at least 4 s that an episode shares with another episode of
the same show (`podcasts.fingerprint`, a spectral-peak fingerprint matcher, finds them). They become the
episode's `repeat` cuts (see above). `list_podcasts` shows how much was cut from each
episode and by what, and the sync logs each episode's cuts to `sync.log`.

- It's on for every podcast unless turned off: `add-podcast(url, scrub_ads=false)`, or ask
  the agent to stop cutting ads from a show. Episodes already cut stay cut.
- What goes: repeated ads (dynamic ads differ between episodes, so an ad is only caught once
  it has run in two of them), ad-break bumpers, and the show's theme where it plays alone.
  Theme music with talk over it stays. A repeat longer than 8 minutes is kept, since that
  is a rerun or a replayed segment.
- Each episode's fingerprint, taken from its original, is kept in
  `~/.local/share/everythingllm/podcasts/prints/<slug>/` (about 2 MB an hour; the newest 10 per show), so a
  new episode is compared with the earlier ones without decoding them again, including
  ones pruned. With `keep` 1, the first episode has nothing to compare with and goes out
  as it is.
- A new episode joins `feed.xml` only once its ads have been looked for, so a podcast app
  never fetches it with its ads. Episodes already in the feed when scrubbing is turned on
  stay in it while they are. One that can't be read goes out as it is, with the error in its
  record.
- Reading an episode takes about 15 s an hour of audio and about 450 MB of memory an hour
  of audio, in the sync on the host. Video episodes aren't cut.

### Transcripts

`podcasts-transcribe.timer` transcribes every downloaded episode with Whisper's `base` model,
newest first, half an hour after each sync. It transcribes the original, and keeps the
segments in its times in `~/.local/share/everythingllm/podcasts/transcripts/<slug>/`. Each transcript is published
as `<episode>.vtt`, named and shifted to match what's served (cut lines left out), and linked
from `feed.xml` (`<podcast:transcript>`), so apps such as AntennaPod show it. The
`search_podcasts(query)` tool searches the kept segments, shifted the same way. It returns
the show, episode and time of each mention: the exact phrase, or else every word within a
few lines of each other.

- It runs apart from the sync, at nice 19, on as many threads as
  `PODCASTS_TRANSCRIBE_THREADS` in `host.env` gives for the time of day: a number, or
  entries like `08:00=4,22:00=1` (all 4 cores by day, fans audible; 1 at night, quiet,
  since on 2 they spin up). Unset, it is 1. A run checks between episodes and loads
  Whisper again when the period changes; 0 threads pauses it until the next entry (the
  run stops, and a later timer run starts again). Whisper is slow on this CPU: about 8
  minutes an hour of audio on every core. After each episode it takes the newest one
  waiting, so a show's new episode goes ahead of a catalog's backlog. It holds no lock while transcribing or looking for ad reads, then takes the sync lock briefly to save the
  result, but only if the episode's original and served file are still the ones it
  transcribed. A second run
  exits at once while one is going (`~/.local/share/everythingllm/podcasts/transcribe.lock`); its output is in
  the journal (`uv run hostctl podcasts-logs`).
- The model (about 150 MB) is downloaded on first use to `~/.local/share/everythingllm/podcasts/models/whisper/base`.
- **Ad reads.** Audio fingerprints miss an ad heard for the first time, or one the host
  reads in their own words, so AnythingLLM's default model (DeepSeek, key and model from
  AnythingLLM's `.env`, through the shared `llm` package) reads each new transcript, half an hour at a time, and names
  the lines each ad runs over: sponsor reads, promos for other shows, ad-free tiers, and
  the show's own Patreon and merch plugs, but not content warnings or credits. A read must
  last 5 s to 6 min; anything else it names is ignored. Without a key, or when its answer
  can't be used, a phrase list does it instead ("brought to you by", "use code",
  "x.com/show" and the like, two within a minute). Per show, `add-podcast(ad_words=...)`
  picks what happens to them. `cut` (the default) makes them active `ad-read` cuts, left out
  of what's served and of the transcript. `report` keeps them as inactive cuts, which
  `list_podcasts` lists as possible sponsor reads. `off` ignores them. Every read
  found is logged with its opening words (`uv run hostctl podcasts-logs`), to check what was cut.
- `add-podcast(transcribe=false)` turns transcripts off for a show. An episode that can't be
  transcribed gets the error in its record and isn't tried again.

To look at what would be cut without cutting it:

    uv run --package podcasts --extra host spot repeats ep1.mp3 ep2.mp3 ep3.mp3

Run a sync by hand (every feed, or one, logging to `~/.local/share/everythingllm/podcasts/sync.log`):

    systemctl --user start podcasts-sync@_all.service
    systemctl --user start podcasts-sync@hard-fork.service

## News articles

A Daily News headline doesn't link to the outside source. It opens an article the bot
writes the first time someone clicks it. The edition links each story to
`/news/write/<day>/<desk>/<n>` (the n-th story of the edition's desk-th section), which
`tailscale serve` maps to the article writer (`sites.articles_web`, served by `sites-runner`
on 127.0.0.1:8448), so the writer shares the news site's origin while Caddy stays static
and read-only. Without a DeepSeek key, `sites-runner` logs that and serves the tools without it.

- If the article is already written, the writer redirects to it at
  `/news/articles/<desk>-<n>-<day>/`. Otherwise it starts writing and answers a page that
  reloads every 4 s until the article is ready (no script, so no CSP problems). Writing
  takes about 5–30 s.
- A link only names a story the daily job saved, by edition day and positions. The
  writer never fetches a URL taken from the request.
- To write a story, it searches SearXNG (127.0.0.1:8888) for the headline, since the job
  often saves a site's front page as the link, and reads the story's link plus the top
  results with trafilatura (up to 4 readable pages). Only public hosts are fetched,
  redirects included. DeepSeek (AnythingLLM's model, thinking off) then writes 250–600 words from the pages that actually report the story,
  or refuses if none do. Its key and model are read from AnythingLLM's `.env`, and only
  those.
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
on the host (`packages/research`, `host/systemd/research-runner.service`, its own venv in
`~/.local/share/everythingllm/venvs/research`), and answers at once with the run's live
progress card, so the chat is free while the run goes. They talk over a Unix socket the
container sees, `storage/everythingllm/research/runner.sock` (see "Services on the host"):
`start` returns a run id and its card, `wait(run_id, since)` long-polls up to 45 s for new
progress lines and the result, `runs` lists what the runner holds.

**The live card.** `start`'s `card` is a Markdown image in a link,
`[![Deep research: <question>](…/_live/research/<id>.png)](…/_live/research/<id>)`, which the
agent pastes as it does a link card. research-runner serves both on 127.0.0.1:8450
(`RESEARCH_LIVE_PORT`, `research.live`), which `uv run hostctl serve-setup` maps to
`https://<PUBLIC_HOST>:8445/_live/research/` with `tailscale serve`. The image is
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
gets one frame of how it ended from the log, the audit's `research_run` hands the card out
again, and an old chat's card still opens the report.

A run, step by step:

1. **Plan** — the planner model splits the question into sub-questions with search queries.
   The calling agent can make the split itself instead: the skill's `sub_questions` (each a
   goal, or `{goal, queries}`, at most the depth's workers) and an optional `title` skip
   this step, and the run log's `stats.plan` says `caller`.
2. **Research** — one worker per sub-question, all in parallel. Each searches SearXNG
   (`http://127.0.0.1:8888/search` on the host), reads pages with
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
   log has the saved file and the error.
7. **Keep a copy** — unless the `EMBED_IN_WORKSPACE` setup
   arg is `no`, it's also stored in `storage/documents/deep-research/` and embedded into
   the workspace that ran it, through AnythingLLM's API as the UI's document picker does
   (`POST /api/workspace/<slug>/update-embeddings`, then a look at the workspace's
   documents, since the native embedder doesn't say what it embedded), so later chats can
   search it. Scheduled jobs have no workspace, so they only save the file. A failure here
   only warns: the reply and the run log (`file_error`, `document_error`) say so.

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
runner, which publishes and embeds the report as usual. A run nobody was watching (its
card, or a `wait`) when it finished gets `chat_closed: true` in the run log. To
find the report, ask in that workspace or open the research site. A run can't be
cancelled from the chat: `FORCE=1 uv run hostctl research-setup` restarts the runner, which kills
every run in it. Runs are bounded by their search budget either way. At most 2 run at once;
another waits its turn, and its progress says so.

Only a restart of `research-runner` kills a run without a result, so while a run is going
it has a marker in `~/.local/share/everythingllm/research/runs/running/<id>.json` (its question and when it
started), touched every minute. When the runner starts, it moves every marker into the
log as status `interrupted`, since none of them can be its own; until then, a marker quiet
for its `stale_ms` (3 minutes) reads as interrupted to the audit, and a fresh one as
`running`. The audit's `research_run(question=...)` finds a run by words from its question,
so the agent can tell the user what happened instead of finding no such run.
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
line. AnythingLLM keeps only a chat's final reply, so this is the record the audit reads.

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
unused).

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
  by a test). A task can read and report; it can't write, run code or delegate again.
- **The live card** is served on 127.0.0.1:8451 (`AGENTS_LIVE_PORT`) and mapped to
  `https://<PUBLIC_HOST>:8445/_live/agents/` by `uv run hostctl serve-setup`. Its page shows the
  progress, and every task's reply once the delegation is done, escaped and under a CSP
  that allows nothing but the page's own CSS (`runs.live`).
- **The run log** is `~/.local/share/everythingllm/agents/runs/` (`runs.runlog`, as
  research's). The audit doesn't read it yet.
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

## MCP gateway

The gateway (`packages/gateway`, `gateway.service`, 127.0.0.1:8452, tailnet https :8452)
serves the runners' tools over MCP's streamable HTTP to clients other than AnythingLLM,
such as Claude Code on another machine on the tailnet (`docs/.proposals/gateway-and-containers.md`,
kept out of git). It's one more front on the host, over the same runner sockets. Its tools
come in groups, and a client gets the groups it's granted. Two tools of the same name stop
it from starting.

- **The fronts' read tools** (groups `sites`, `podcasts`, `audit`). It imports
  `sites.server`, `podcasts.server` and `audit.server` and serves each one's
  `tool.registered` (what `hostrpc.forwarder` registered), so the schemas and docstrings are
  the ones AnythingLLM sees.
- **The fronts' skills as tools** (`sites:write`, `podcasts:write`, `audit:write`):
  `write_entry`, `delete_entry`, `add_podcast`, `remove_podcast`, `publish_report` and
  `run_job`. The gateway wraps each front's `skills` (signatures, as its tools are) with
  `hostrpc.forwarder` itself, so each call goes to the front's runner under the op's name,
  as the generated skill's does.
- **Fronts declared in the gateway** (`agents`, `research`, `sandbox`): `gateway/agents.py`,
  `gateway/research.py` and `gateway/sandbox.py`, declared like a front's tools (signatures
  with docstrings) but never run as MCP servers of their own. Each names its tools with its
  `PREFIX` (`agents_`, `research_`, `sandbox_`), so their `wait`s and `runs` don't clash;
  the op sent to the runner keeps its own name (`delegate`, `start`, `run`, …).
- **Delegation** (`agents`): `agents_delegate`, `agents_wait`, `agents_runs` and
  `agents_cancel` over agents-runner. A client follows a run with `agents_wait`, advancing
  `since` by the events it got, until `done`. The daily budget (`AGENTS_DAILY_USD`) counts
  these delegations too.
- **Deep research** (`research`): `research_start(question, depth, sub_questions, title)`,
  `research_wait(run_id, since)` and `research_runs()` over research-runner. A run started
  here has no workspace (`workspace` None, `embed` false, whatever the arguments say), so
  its report is published to the research site and saved to the runner's files, and added to
  no workspace's documents; the models are the runner's defaults, not the deep-research
  skill's setup args. `research_start` answers at once with `{run_id, queued, card}`; a
  client follows the run with `research_wait` as with `agents_wait`, and once it's done
  the result's `url` ends in the report's slug, which it reads with
  `get_entry(site="research", section="reports", slug)` (the `sites` group).
- **The code sandbox** (`sandbox`): `sandbox_run(language, code, timeout)`,
  `sandbox_wait(run_id)`, `sandbox_write(path, content, delete)`,
  `sandbox_publish(slug, path, remove)` and `sandbox_build_site(path, slug)` over
  sandbox-runner, the ops behind `run-code`, `write-file`, `publish` and `build-site`. Each
  call carries the scope `{workspace: "client-<name>", thread: "gateway"}`, made from the
  calling client's name (`gateway.grants.client`), never from the model's arguments: a
  `scope` argument is dropped, and the gateway's scope is the one sent. So a client has a
  sandbox workspace of its own, `client-<name>`, with one thread: its `/work` is
  `workspaces/client-<name>/threads/gateway`, and its pages are
  `https://<PUBLIC_HOST>:8447/client-<name>/`. A run or a build answers within the runner's
  45 s wait; one still going comes back as `{run_id, running: true, seconds}`, and the
  client calls `sandbox_wait` until it's done. `sandbox_run` and `sandbox_build_site` wait
  again themselves only while another 45 s wait fits in the call's 55 s (hostrpc's call
  timeout), so a call never runs past what an MCP client's own 60 s limit allows.
- **Sockets.** The fronts' `hostrpc.caller` falls back to the container's storage path, so
  at start the gateway sets each front's `<FRONT>_SOCKET` to the host's
  (`hostrpc.socket_path`), unless it's set already.

What the scopes don't do, by design (one user, so documented rather than enforced):

- Research runs aren't per client. `research_wait` and `research_runs` see every run
  research-runner holds, AnythingLLM's included, and a client can follow any of them.
- A `client-<name>` sandbox workspace is a workspace like any other: its runs read every
  AnythingLLM workspace's `/shared/<workspace>` (read-only), and every workspace's runs
  read its `/shared/client-<name>`. Its `/project`, `/work` and `/public` are its own, and
  count toward its own size limit. An AnythingLLM workspace whose slug is `client-<name>`
  would share that client's folders, so don't give one that name.

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

The relay (`packages/relay`, `relay.service`, 127.0.0.1:8446, tailnet https :8446) makes that
one call for Nilson and owns the answer. Each run streams from AnythingLLM to the end in its
own task, which no follower owns; the relay never closes the upstream connection because a
follower left, only when the run ends or is cancelled.

For everything else (workspaces, threads, history, documents, settings) the relay is
AnythingLLM's developer API: `/api/v1/...` is passed through to AnythingLLM (`relay.proxy`)
with the relay's token swapped for the developer API key. So any AnythingLLM client, of any
version, can be pointed at the relay with `RELAY_TOKEN` as its API key, and the key itself
stays on the host. The request and the answer go through as they are (method, path, query,
body, headers, status, streamed and still-encoded body) but for the hop-by-hop headers; the
`Authorization` header is replaced, not passed on. AnythingLLM being unreachable is a 502
with the usual `{"error"}`; a client that leaves closes its call, so a `stream-chat` made
through the proxy still dies with its client; only a run outlives it. Nothing outside
`/api/v1/` (AnythingLLM's own web UI and its internal `/api/...`) is proxied. The token
therefore grants all the developer API key does, admin endpoints included.

Every route but `/health` needs `Authorization: Bearer <RELAY_TOKEN>`; errors are
`{"error": "..."}`.

| Route | Does |
| --- | --- |
| `POST /runs` | `{"workspace", "thread", "clientId", "body"}` starts a run: 201 with the run. `body` is what the client would send `stream-chat` (a non-empty `message`, or `"reset": true` to clear the thread; `mode` and `attachments` as AnythingLLM takes them), forwarded as it came: without `mode` the workspace's own mode answers, `automatic` included. The body is held only in memory for the call, so attachments never reach the database, and no size limit is set (a 20 MB attachment goes through). A `clientId` already used answers 200 with that run and starts nothing; a thread with a running run answers 409. |
| `GET /runs?status=running` | runs with that status (`running`, `done`, `failed`, `cancelled`), oldest first; every kept run without `status` |
| `GET /runs/{id}` | the run (`id`, `clientId`, `workspace`, `thread`, `mode` (the body's, or null), `status`, `createdAt`, `finishedAt`); 404 when unknown or expired |
| `GET /runs/{id}/events` | server-sent events: `chunk` with each chunk AnythingLLM sent, as it came and in order (an agent's `agentThought`s, the closing chunk and the `finalizeResponseStream` with its sources included), then one of `done` `{}`, `failed` `{"error"}` (for a non-2xx answer, an `error` or `abort` chunk, which isn't passed on, or a broken connection), `cancelled` `{}`, and the stream closes. Ids count from 1; `Last-Event-ID: n` starts after n. `: ping` every 15 s while live. Any number of followers. |
| `POST /runs/{id}/cancel` | closes the upstream connection and ends the run `cancelled`; a run that has ended is left as it is |
| `/api/v1/...` (any method) | AnythingLLM's developer API, through the relay with its key |
| `GET /health` | 200, no token |

Runs and their events are in SQLite (`~/.local/share/everythingllm/relay/relay.db`, mode 600),
written as each event arrives. A restart fails the runs it cut short with "The relay
restarted during the answer." and keeps their events; finished runs are deleted after 7
days (`RUN_RETENTION_DAYS`). The schema's version is SQLite's `user_version`; opening an
older database deletes its runs. With `NTFY_URL` set, a finished or failed run posts "Answer
ready" or "Answer failed" to that ntfy topic, with the question's first 120 characters
and `run=…,workspace=…,thread=…` as its tags; never the answer. A reset isn't notified.

The secrets live in `~/.config/everythingllm/relay.env` (mode 600), outside the repo, which the
AnythingLLM container mounts: `ANYTHINGLLM_API_KEY` (a developer API key), `RELAY_TOKEN`,
and optionally `NTFY_URL` and `NTFY_TOKEN`. `uv run hostctl relay-setup` makes the file with a fresh
token, refuses to go on until the API key is filled in, then maps the tailnet port and
starts the unit; `uv run hostctl relay-logs` follows it (any app's `<app>-logs`). `relay.app`'s docstring lists the rest of the
config. Neither the key nor the token appears in a response or a log line, and a test holds
that.

Where it differs from the original spec: the relay adds nothing to the body and doesn't
interpret the answer (no `mode` default, no pieces or citations of its own); a
connection to AnythingLLM that breaks mid-answer, or ten silent minutes, fails the run
("The connection to AnythingLLM broke during the answer.") rather than completing it with
what came; and a `clientId` is remembered as long as its run is kept, so reusing it later
returns that old run.

## System audit

The "System Audit" scheduled job checks this setup every day and publishes what it finds
to the `status` site. The checks and the report are fixed code in the `audit` MCP server,
which forwards each tool call to `audit-runner` on the host (`packages/audit/src/audit/tools.py`,
`host/systemd/audit-runner.service`, socket `storage/everythingllm/audit/runner.sock`), where the checks
can read the journal and every service's socket; the model only writes a summary and
suggests a fix per finding. Its tools:

- `run_checks(since_hours)` — every check, numbered findings grouped as fail / warn / info.
  Fail and warn come in full; info is trimmed to the 5 a report keeps (taken an area, then
  a title, at a time), one line each:
  - logs: error-like lines from AnythingLLM, SearXNG, the pages Caddy,
    and the host services (the apps' units, `WATCHED` in `audit/services.py`) in the host journal, grouped with counts (SearXNG's per-engine errors become
    counts per engine; known noise is skipped, see `NOISE` in `checks.py`);
  - search: a test query to SearXNG, and which engines refuse it;
  - services: every app's runner (`RUNNERS` in `audit/services.py`) answers `ping` on its
    socket, and the sandbox runner with no problems (its image, network and proxy are up);
  - llm: when `LLM_PROVIDER` is openrouter, what's left on the key's limit and the account
    (OpenRouter's `/api/v1/key` and `/api/v1/credits`, with `OPENROUTER_API_KEY` from
    AnythingLLM's `.env`): warn under $2, fail under $0.25 (`AUDIT_CREDIT_WARN_USD`,
    `AUDIT_CREDIT_FAIL_USD`), since out of credit every chat fails with 402;
  - jobs: scheduled-job runs from AnythingLLM's API, judged by what they did rather than
    what they said: failed or timed-out runs (AnythingLLM keeps nothing of a timed-out run,
    so the finding gives only how long it ran), tool calls that returned errors, runs with no
    successful tool call, replies that end in raw tool-call markup (the model stopped mid
    call), and enabled jobs that missed a run (no run started at or after their
    `nextRunAt`, which AnythingLLM leaves at the time a job last ran);
  - research: deep-research runs from their run logs: failures, unpublished reports, failed
    searches, a failed fact-check, workers that found nothing or hit dead search;
  - sites: every site's home page and each section's newest entry answer 200, no entry file
    is newer than the site's last build (its `.zola-site` marker; a write that saved but
    didn't rebuild), plus each section's `[extra.audit]`, with ages in Stockholm days.
- the `publish-report` skill (`publish_report(summary, suggestions, status?)` on the
  runner) — writes the day's report to
  `status/reports/YYYY-MM-DD` (the Stockholm date) through the sites store and build: the
  findings from this run's `run_checks` (or a fresh run when there's none from the last
  hour), the model's summary, and its
  suggestions keyed by finding number. The `reports` section is `agent_readonly`, so only
  this op writes it.
- `journal_lines`, `job_run`, `research_run` — the raw material behind a finding
  (`research_run` gives the run's last 30 progress lines).
- the `run-job` skill (`run_job(name)`) — runs an existing scheduled job now (AnythingLLM's
  `POST /scheduled-jobs/:id/trigger`) and returns the run id. It's for chat ("redo today's
  news"), so the agent doesn't make one-off cron jobs; the audit job never calls it.

The checks all run in one tool call, which AnythingLLM gives up on after 60 s, so
journalctl and each ping stop after 20 s (`COMMAND_SECONDS`, `PING_SECONDS`).

Run the checks by hand on the host, as audit-runner does:

    uv run --package audit python -c 'from audit.tools import run_checks; print(run_checks(24))'

## SearXNG

The agent's web search goes to a private SearXNG, a metasearch engine: each query fans
out to Google, Bing, Wikipedia and others and the merged results come back as JSON.
Nothing is indexed locally and no API keys are needed.

SearXNG is deployed by Ansible, not from this repo: the Quadlet unit
`searxng.container` and its config in `/srv/searxng/settings.yml` (JSON output on,
limiter off). It listens on 127.0.0.1:8888, and `tailscale serve` maps tailnet HTTPS :8888
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
