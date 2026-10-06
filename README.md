# EverythingLLM

Source of truth for the local AnythingLLM instance (`anythingllm.service`, a Quadlet unit
rendered from `host/quadlet/` into `~/.config/containers/systemd/`, storage in
`ANYTHINGLLM_STORAGE`, on this machine `/srv/anythingllm/storage`).

## Setting up a machine

On a new machine, or to bring this one up to date:

    git clone <repo> && cd everythingllm         # any folder; the units are rendered with its path
    cp host.env.example host.env && $EDITOR host.env
    make install

`make install` first checks the machine and stops with a list of what's missing. It checks
`host.env`, the tools the units run (podman, uv and zola at fixed paths), lingering,
tailscale, and the storage folder, creating the folders the containers mount inside it.
Then it does the following:

1. renders and starts the units (`make units`)
2. waits for AnythingLLM
3. deploys (`make deploy`)
4. points web search at SearXNG
5. runs every setup target: tailnet ports, sandbox, podcast
   and read-aloud timers, research, sites and audit runners
6. runs `make health`
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

- the Makefile, which exports both values
- `tools/sync.py`, for `ANYTHINGLLM_STORAGE`
- the host's systemd units, through `EnvironmentFile=@REPO@/host.env` (filled in by `make units`)
- the site builds, which read `PUBLIC_HOST` from it and pass zola
  `--base-url https://<PUBLIC_HOST>:8445/<site>`, so `zola.toml` doesn't name the host.
  They find the file at the root of the repo the sites are in, and the container sees it at
  `/mcp/host.env`, so builds that get none of our environment, like a script's
  `sites-write`, use it too.

Code running on the host derives its storage paths from `ANYTHINGLLM_STORAGE`. Inside the
container that variable isn't set, and storage is `/app/server/storage`. Tests ignore
`host.env`, so they run the same on any machine.

### Containers and tailnet ports

This repo owns the two containers the setup runs, as templates in `host/quadlet/`:

- `anythingllm.container`: AnythingLLM, pinned by digest, because the log filter depends on
  its internals
- `static_agent.container`: the pages site, a Caddy container that mounts
  `host/caddy/pages.Caddyfile` from the repo, so its CSP is versioned. Everything gets
  `default-src 'self'; script-src 'none'`: no scripts, and nothing fetched from another
  host, so CSS can't send anything out either. `form-action 'none'; base-uri 'none'` cover
  what `default-src` doesn't: no form posts anywhere, and no `<base>` repoints a page's
  links. A page the sandbox published (a folder with a `.page` file naming the workspace
  it belongs to, and the root's listing) may carry inline CSS too
  (`style-src 'self' 'unsafe-inline'`). Everything unmarked keeps it blocked, so a new site
  or folder starts strict: the Zola sites, `/podcasts`. Caddy hides the `.page` and
  `.zola-site` markers, and a test holds both policies. `publish` warns the agent when a
  page uses something the CSP blocks (scripts, stylesheets, fonts or images from other
  hosts), since the page would otherwise just render without it.

The host's own units in `host/systemd/` (services, timers, and the AnythingLLM drop-in) are
templates too. `make units` renders all of them:

- `host/quadlet/*.container.in` goes to `~/.config/containers/systemd/`
- `host/systemd/*.container.d/` goes next to it
- `host/systemd/*.service` and `*.timer` go to `~/.config/systemd/user/`

It fills in `@REPO@` (the checkout's path) and the `host.env` settings, and saves older
versions to `~/.local/share/everythingllm/backups/`. Then it reloads systemd and restarts what changed: a container
whose unit or drop-in changed, or a host unit that's running. A change to comments alone
restarts nothing. Enabling a host unit is up to its `make *-setup` target.

Run it from the main checkout. It refuses to run in a worktree, since the units run the
repo they were rendered from. Edit the templates, never the installed copies; `make diff`
shows where the two differ.

An Ansible playbook used to install the two containers' units and `/srv/static-agent-config/`.
It must leave them alone now, or its next run undoes `make units`.

`make serve-setup` maps this setup's tailnet ports with `tailscale serve`: the pages site on
:8445 (with `/news/write` to the article writer), SearXNG on :8888, AnythingLLM's UI on
:3001 and the Nilson relay on :8446. Other mappings on the machine are left alone.

Not in this repo, so a new machine needs them first: rootless podman with Quadlet, systemd
lingering for the user, tailscale, uv, zola in `/usr/local/bin`, SearXNG (deployed by Ansible,
see SearXNG below) and Ollama if it's the embedding provider. AnythingLLM's own settings
(providers and keys in its `.env`, workspaces, which built-in skills are off) are set
through its UI.

## Layout

- `anythingllm/agent-skills/<hubId>/` — custom agent skills (`plugin.json` + `handler.js`)
  - `deep-research/` — multi-source web research with GLM and DeepSeek, published to the
    `research` site; hands the work to `research-runner` on the host (see "Deep research")
  - `run-code/`, `write-file/`, `publish/` — the code sandbox, run by `sandbox-runner` on
    the host (see "Code sandbox")
  - `_lib/` — what the skills share (no `plugin.json`, so AnythingLLM doesn't load it as
    a skill): `hostrpc.js`, the node side of `packages/hostrpc`, and `sandbox.js`
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
- `zola/` — static sites built with Zola from entries the agent writes (see below)
- `packages/` — MCP servers we write: members of the uv workspace at the repo root
  (`pyproject.toml`, `uv.lock`), one per subdirectory
  - `packages/sites/` — the Zola sites on the tailnet pages site (:8445): list/write/get/delete
    their entries and build them; and `headlines(section)`, the last 30 hours' stories for
    the Daily News job from the feeds in `FEEDS` (`sites/feeds.py`), each with its own link.
    The MCP server forwards to `sites-runner` on the host, which does the work
  - `packages/audit/` — health checks over this setup, for the System Audit job (see below); the
    MCP server forwards to `audit-runner` on the host, which runs them
  - `packages/sandbox/` — not an MCP server: `sandbox-runner` runs the agent's Python and bash
    in throwaway podman containers on the host, with only PyPI on the network, and
    publishes pages from them, for the `run-code`, `write-file` and `publish` skills (see
    "Code sandbox" below)
  - `packages/podcasts/` — downloads podcast episodes, finds their ads, and serves them without
    those as private feeds on the pages site (see "Podcasts" below); the MCP server forwards
    to `podcasts-runner` on the host, which does the work. Its audio code is here too:
    `podcasts.avio` (PyAV decoding, and cutting without re-encoding), `podcasts.fingerprint`
    (finds the stretches recordings share), `podcasts.whisper` (speech to text, with the
    transcripts' types in `podcasts.segments`) and `podcasts.speech` (text to speech with
    Kokoro, for the Daily News read aloud). `podcasts.cli` runs them by hand as `spot
    repeats`, `transcribe` and `speak`, with the models in `~/.local/share/everythingllm/podcasts/models` as the
    services use them
  - `packages/hostrpc/` — a library, not a server: how the MCP servers and skills talk to the
    services on the host (see "Services on the host" below)
  - `packages/splice/` — not an MCP server: `splice-web` serves the podcasts, putting each episode
    together from the untouched download and the stretches to leave out (see "Originals,
    cuts and podcasts-web" below)
  - `packages/research/` — not an MCP server: `research-runner` runs the deep-research skill's
    runs on the host, and `research-run` runs one by hand (see "Deep research")
  - `packages/publicweb/` — a library, not a server: the HTTP client podcasts, sites and research use,
    which refuses LAN, tailnet and loopback hosts, and `publicweb.pages`, the page reader on
    it that the article writer and research share
  - `packages/linkcard/` — a library, not a server: draws the link cards the chat shows for a
    published page (see "Code sandbox")
- `packages/relay/` — the Nilson relay, a host service for the Nilson chat app rather than for
  AnythingLLM's agent; also a workspace member (see "Nilson relay")
- `host/systemd/` — host user units, rendered into `~/.config/systemd/user/` (`make units`);
  each one's `Description=` says what it does, and its `make <name>-setup` target installs it.
  `anythingllm.container.d/` is a Quadlet drop-in that preloads `anythingllm/log-filter.js`
  to cut MCP payloads from AnythingLLM's log.
- What only host services read or write lives in `~/.local/share/everythingllm`
  (`hostrpc.data_dir()`), not in AnythingLLM's storage, which the container mounts. It's
  laid out by kind:

      venvs/<name>/        the host services' venvs (audit, podcasts, relay, research,
                           sandbox, sites, splice)
      pages/public/        the pages site Caddy serves
      pages/entries/       the Zola entries
      sandbox/workspaces/  the sandbox's folders, one per workspace
      sandbox/shared/      /shared, every workspace's
      podcasts/            the podcasts' state, audio, transcripts and manifests
      podcasts/models/     Whisper and Kokoro
      research/runs/       the deep-research run log and live runs' markers
      relay/               the Nilson relay's database

  Storage keeps AnythingLLM's own data, the runners' sockets (`storage/<name>/runner.sock`,
  which the container reaches) and what AnythingLLM reads (`anythingllm-fs/research/`,
  `documents/`).
- The `static_agent` Caddy container mounts just `pages/public/` read-only and serves it on
  127.0.0.1:8445
- `host/quadlet/` — the AnythingLLM and pages-site Quadlet units, as templates (`make units`)
- `host/caddy/pages.Caddyfile` — the pages site's Caddy config, including its CSP
- `tools/sync.py` — diff/deploy/import between this repo and live storage; standard
  library only, run with the system `python3`
- `tools/units.py` — renders and installs `host/quadlet/` and `host/systemd/` (`make units`)
- `tools/machine.py` — `make install`'s checks, its wait for AnythingLLM, the web search
  setting and the closing checklist

## Workflow

`make help` lists every target. Day to day: `make diff` shows what would change live,
`make deploy` copies it into storage (old files go to
`~/.local/share/everythingllm/backups/`), refreshes the MCP deps
and restarts AnythingLLM, `make test` runs every test and `make health` checks every unit,
port, host service and MCP server. `make import-skill NAME=<hubId>` (and `import-job`,
`import-command`) brings something made in the UI under the repo.

Skill handlers are re-required on each load, so skill changes don't need a restart, but
`make deploy` also runs `make mcp-sync` and `make restart`, so AnythingLLM and every MCP
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
    uv run --all-packages --all-extras pytest -q   # all tests (what `make test` runs)
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
  which installs that member's base dependencies, so Whisper, Kokoro and PyAV stay out of
  the container.
- After `uv.lock` changes, run `make mcp-sync` (or `make deploy`, which runs it) so the
  container's venv catches up. It installs exactly the members `mcp_servers.json` runs
  (`tools/sync.py mcp-packages`), and removes anything else.

## Zola sites

For sites an agent keeps adding to, the agent writes entries, not HTML, and Zola does the
rest. `zola/themes/agent-site/` is the shared theme (layout, entry lists, a year-grouped
archive, Atom feed; no scripts or inline styles, so it passes the CSP). Each
`zola/sites/<name>/` is one site: `zola.toml` (its `base_url` is `…:8445/<name>`), section
`_index.md` files, and any templates or `static/` CSS it overrides or adds. Templates are
Tera 2: reusable pieces are `{% component %}`s (global, no import), not macros.

The `sites` MCP server writes entries to `~/.local/share/everythingllm/pages/entries/<name>/<section>/<slug>.md`
(JSON front matter, fields under `extra`) and then rebuilds that site itself, so an entry
is live when `write_entry` returns; if the site doesn't build, the write or delete is
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

The sites MCP server's builds run on the host, in `sites-runner`, and the audit's report
builds in `audit-runner`, both with the host's zola, as every other writer's do; nothing
in the container builds. Other writers use the
`sites-write` command (entry as JSON on stdin; it saves, builds and prints the URL, or
exits 1 with `{"error"}` and keeps nothing when the site doesn't build), so the
entry format has one implementation. The Python writers (the article writer, research-runner)
call `SiteStore` directly instead.
Templates, stylesheets, `zola.toml` and sections change only in the repo; the agent has no
tool for them. `make deploy` rebuilds every site on the host (`make sites-build`), so
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

A new site: add `zola/sites/<name>/` with `theme = "agent-site"`, its sections and an
`agent_help`, run `make deploy`, and point a job or chat at the `sites` server.

## MCP servers in AnythingLLM

The repo is mounted read-only into the AnythingLLM container at `/mcp` (see the
`Volume=` line in `host/quadlet/anythingllm.container.in`), and `mcp_servers.json` launches each server
with `uv run --frozen --project /mcp --package <name>`. The container's venv and uv
cache live in `/srv/anythingllm/storage/mcp/`. The container can't reach the host's
loopback, so servers run inside it over stdio rather than as host HTTP services.

### Services on the host

Work that is heavy, long or needs the host goes to a service on the host instead, with the
MCP server or skill in the container as a thin front: `sandbox-runner` (the code sandbox),
`research-runner` (deep research), `podcasts-runner` (the podcasts tools),
`sites-runner` (the sites tools and their builds) and `audit-runner` (the audit's
checks). Each listens
on a Unix socket in storage, `storage/<name>/runner.sock` (mode 0660), which the container
sees without a Quadlet change, and they all speak `hostrpc`'s protocol: one request per
connection, a line of JSON each way, `{"op", "args"}` in and `{"ok": true, "result"}` or
`{"ok": false, "error"}` out.

- `hostrpc.Service(ops, errors=…)` dispatches each request to the function of that name in
  `ops` (a package's `tools.OPS`) or to an `op_<name>` method of a subclass (research,
  sandbox), running one that isn't a coroutine in a thread; a `hostrpc.RunnerError` becomes
  the error the caller sees, as does that of the service's own `errors` (sites-runner's
  `SiteError`); anything else is logged and reported as `runner error: …`. Every service answers `ping`, which
  `make health` and the audit ask. `hostrpc.serve` serves one on its socket and removes the
  socket on SIGTERM, or when a `stop` event is set; `hostrpc.run` is a runner's `main()`
  around it, and `hostrpc.serving` serves one for the length of a test.
- `hostrpc.request(socket, op, args, timeout, name=…)` asks one, raising `RunnerError`
  (also when nothing listens). An MCP server gets its `call(op, args)` from
  `hostrpc.caller(folder, env, name, error=ToolError)`, which turns that into a tool error,
  and `hostrpc.forwarder(call, mcp.add_tool)` makes each tool from a signature and docstring
  alone: calling it sends every argument as the op of its name. The skills speak the same
  protocol from node (`anythingllm/agent-skills/_lib/hostrpc.js`).
- AnythingLLM gives up on a tool call after 60 s, so an op answers within 45 s, and work
  that takes longer carries on in the service (a run id to wait on) or in a unit of its own.
- The container maps the host user (`UserNS=keep-id`), so what a service writes in storage
  is the container's to read and the other way round, and file locks work across both.
- A new one: an `OPS` tuple and a `main()` that calls `hostrpc.run` in the package's
  `tools.py` (`packages/podcasts` is the example), a `<name>-runner` console script, a unit
  `host/systemd/<name>-runner.service` with its own venv in `~/.local/share/everythingllm/`, and
  an entry in the audit's `WATCHED` (`audit/services.py`), which `RUNNERS` and a test
  follow. Its socket is `storage/<name>/runner.sock`, where `hostrpc.caller` looks.

Code edits go live the next time AnythingLLM starts the server (restart it from the
Agent Skills > MCP Servers page, `make restart`, or `make deploy`, which restarts). Note
that this runs whatever is in the working tree, committed or not. Requires `mcp` 2.x (`MCPServer`, not `FastMCP`).

`tailscale serve` maps tailnet HTTPS :8445 to the pages site. Pages live under
AnythingLLM's storage, so the container sees them without another mount.

## Code sandbox

Three agent skills give the agent a small Linux machine to run code in, like the Claude
app's, and a way to publish what it makes:

- `run-code` runs a Python or bash script and replies with its output. It waits for the
  whole run (up to 300 s), showing in the chat that it's still going; skills, unlike MCP
  tools, have no 60 s limit. Reading, listing, moving and deleting files is bash.
- `write-file` writes a text file, or deletes a file or folder (deleting `/work` or
  `/project` empties it).
- `publish` copies a file or folder to `/<slug>/` on the pages site (:8445), or takes a
  page down. An HTML file becomes the page; a folder goes whole, with its `index.html` as
  the page. The page belongs to the workspace that published it: only that workspace can
  replace or remove it, and a slug that's taken by anything else (another workspace's
  page, a Zola site, `/podcasts`) is refused. The root's `index.html` lists every page.

**Link cards.** AnythingLLM's chat shows a Markdown image up to 800 px wide, and keeps it a
link when it's inside one, even with "Render HTML in chat" off. So whatever publishes a page
(`publish`, the sites tools' `write_entry`, deep research) also has `linkcard` draw a card of
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
over a Unix socket, `storage/sandbox/runner.sock` (see "Services on the host"), to
`sandbox-runner` on the host (`host/systemd/sandbox-runner.service`, its own venv in
`~/.local/share/everythingllm/venvs/sandbox`).

**Scopes.** A call carries where it came from, which AnythingLLM gives the skill and the
model never chooses: the workspace (`_jobs` for a scheduled job, which has none) and the
chat thread (`default` for a workspace's main chat, and for API, Telegram and job runs).
Each run mounts:

- `/work`: the thread's scratch folder, and where a run starts. It's deleted 7 days after
  the thread last used the sandbox.
- `/project`: the workspace's folder, shared by its threads and kept until deleted. `pip
  install`s go to `/project/.local`, so they last too. To keep a file, move it here.
- `/shared`: one folder every workspace (and `_jobs`) reads and writes, kept until deleted.
  It holds the lab site (below). It's mounted `noexec,nosuid,nodev` and never on `PATH`, and
  the skills and system prompt tell the agent to treat it as data, since another
  workspace's chat may have written it: it's the one place where a prompt injection in one
  workspace can reach another.
- `/pages`: the workspace's published pages and the shared ones, read-only, so `ls /pages`
  lists them.

They live in `~/.local/share/everythingllm/sandbox/workspaces/<workspace>/` (`project/` and
`threads/<thread>/`) and `~/.local/share/everythingllm/sandbox/shared/`, out of the container's
reach. `/shared` is held to 2 GB on its own. A workspace's folders together are held
to 5 GB: over that, runs and writes are refused until the agent deletes something with
`write-file`, and the refusal names the biggest files and folders, since no run can look
for them. A run warns past 4 GB. Runs take turns, across workspaces too, since every run
can write to `/shared`; while one is going, a write or publish from any of its workspace's
chats fails at once rather than waiting, and so does one under `/shared` from any
workspace (otherwise a run could swap a symlink in under a path the call just resolved).

**Shared pages.** A page published from `/shared` belongs to every workspace: its `.page`
marker names `/shared` as the owner, any workspace can republish it from `/shared` or take
it down, and none can take its slug over from its own folders (nor can `/shared` take a
workspace's).

**The lab site** is the one site the agent controls entirely: templates, stylesheets,
`zola.toml` and content, in `/shared/sites/lab/`. It started as a copy of the
`agent-site` theme and a welcome entry, with a `README.md` for the agent and a git
repository so it can roll back. The sandbox image has the host's zola version; a run
builds it (`zola build`, no network as always) and `publish` puts it at
`https://<PUBLIC_HOST>:8445/lab/`. Nothing in the repo or on the host reads it, so it can
break without breaking anything else, and the CSP still holds for whatever it serves.

**Each run** gets a fresh `localhost/everythingllm-sandbox` container, with the script mounted
read-only from a host-only folder at `/sandbox`:

- non-root (`--userns keep-id`), read-only root, `--cap-drop ALL`, `no-new-privileges`;
- 1 CPU, 1 GB memory, 256 processes, 60 s by default (300 s max), then killed; a run
  that hits the memory limit is reported as such (podman's `OOMKilled`);
- output clipped to the first and last part; at most 2 runs at once;
- containers carry the label `everythingllm-sandbox=1`; the runner removes any left over
  from a crash or restart when it starts.

The host never follows a symlink out of a mount when it reads, writes or publishes for the
agent, won't write into a FIFO or device there, leaves symlinks out of a published folder,
and won't publish over a symlink planted in the site folder; the sandbox can create any
symlink it likes in its own folders.

**Network.** Sandboxes sit on `sandbox-net`, a podman network made with `--internal`
(no route out) and `--disable-dns` (no DNS, so nothing leaks out through lookups either). Their
only way out is `sandbox-proxy` (tinyproxy, `host/systemd/sandbox-proxy.service`), which is
also on the default network and lets through only the hosts in
`host/containers/sandbox/allowlist`: `pypi.org` and `files.pythonhosted.org`. So `pip
install` works, and the internet, the LAN, the tailnet (AnythingLLM's API, Ollama, …)
and the host's own ports don't. `upload.pypi.org` stays blocked, so code can't push
data out through a package upload either. To allow another host, add an anchored regex to
`allowlist` and run `make sandbox-setup`, which rebuilds the proxy image.

The `logs` and `services` audit checks cover both units and ping the runner.

## Podcasts

The `podcasts` MCP server keeps private copies of podcasts: `add_podcast(url, keep)`
subscribes to a show's RSS feed, and its newest `keep` episodes (default 5, up to 100, or
`"all"` for the whole catalog) are downloaded to `~/.local/share/everythingllm/podcasts/audio/` and listed in a
feed of our own, `https://<PUBLIC_HOST>:8445/podcasts/<slug>/feed.xml`, which podcasts-web
serves (range requests included, so players can seek). `/podcasts/` lists every feed. Ask the
agent, e.g. `@agent download the last 10 episodes of Hard Fork`, then paste the feed URL
into a podcast app. Other tools: `find_podcast`, `list_podcasts` (downloads, progress,
errors), `refresh_podcasts`, `remove_podcast` (deletes the downloads).

`find_podcast(query)` turns a show's name, an Apple Podcasts link, the show's website or a
feed URL into feed URLs for `add_podcast`. Names go to Apple's podcast directory (the iTunes
Search API, no key), Apple links are looked up by their id, and web pages are read for their
`<link rel="alternate" type="application/rss+xml">`. Every candidate is fetched and parsed
first, and the reply lists each working feed with its title, author, episode count and
latest episode; the whole call, checks included, has 45 s (as does `add_podcast`'s fetch),
inside AnythingLLM's 60 s tool limit. A page with no feed link (Spotify,
Amazon Music, Audible and iHeart pages never have one) gets a note to search by name, since
a show found only in such an app has no public feed.

- Use an app that fetches feeds from the phone itself (AntennaPod, Podcast Addict), with
  Tailscale on. Apps that fetch through their own servers (Pocket Casts, Overcast, Apple
  Podcasts' sync) can't reach a tailnet address.
- The MCP server in the container only forwards each tool call to `podcasts-runner` on the
  host (`packages/podcasts/src/podcasts/tools.py`, `host/systemd/podcasts-runner.service`, its
  own venv in `~/.local/share/everythingllm/venvs/podcasts`, socket `storage/podcasts/runner.sock` (the rest of its data is in `~/.local/share/everythingllm/podcasts/`);
  see "Services on the host"), which runs the tool and sends back its text.
  The feeds, the model's key and the audio stack never touch the container: the sync,
  transcription and the read-aloud run on the host too, from the same venv.
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
- `add_podcast(rules="...")` says in plain words which episodes to download: "skip the
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
- `podcasts-sync.timer` runs the sync every 6 hours (`make podcasts-setup`; it replaced
  a scheduled job that only called `refresh_podcasts`, so no agent is involved). A sync
  killed midway (a reboot, or `make units` changing its unit while it runs) leaves its
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
  `remove_podcast` deletes the show's at once.
- `splice-web` (`packages/splice`, standard library only, `host/systemd/podcasts-web.service`,
  its own venv in `~/.local/share/everythingllm/venvs/splice`) is mapped to `:8445/podcasts` by
  `tailscale serve`, ahead of the pages site's Caddy. It serves manifests with range
  requests, `HEAD`, `ETag`/`If-Range` and `sendfile`. Anything else under
  `~/.local/share/everythingllm/pages/public/podcasts/` (feeds, transcripts, the index) it serves as a file, with the
  pages site's CSP and `nosniff`, never following a symlink or leaving that folder.
  `make health` checks it, and the audit reads its journal.
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

- It's on for every podcast unless turned off: `add_podcast(url, scrub_ads=false)`, or ask
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
  the journal (`make podcasts-logs`).
- The model (about 150 MB) is downloaded on first use to `~/.local/share/everythingllm/podcasts/models/whisper/base`.
- **Ad reads.** Audio fingerprints miss an ad heard for the first time, or one the host
  reads in their own words, so AnythingLLM's default model (DeepSeek, key and model from
  AnythingLLM's `.env`, through the shared `llm` package) reads each new transcript, half an hour at a time, and names
  the lines each ad runs over: sponsor reads, promos for other shows, ad-free tiers, and
  the show's own Patreon and merch plugs, but not content warnings or credits. A read must
  last 5 s to 6 min; anything else it names is ignored. Without a key, or when its answer
  can't be used, a phrase list does it instead ("brought to you by", "use code",
  "x.com/show" and the like, two within a minute). Per show, `add_podcast(ad_words=...)`
  picks what happens to them. `cut` (the default) makes them active `ad-read` cuts, left out
  of what's served and of the transcript. `report` keeps them as inactive cuts, which
  `list_podcasts` lists as possible sponsor reads. `off` ignores them. Every read
  found is logged with its opening words (`make podcasts-logs`), to check what was cut.
- `add_podcast(transcribe=false)` turns transcripts off for a show. An episode that can't be
  transcribed gets the error in its record and isn't tried again.

To look at what would be cut without cutting it:

    uv run --package podcasts --extra host spot repeats ep1.mp3 ep2.mp3 ep3.mp3

### The Daily News, read aloud

`news-audio.timer` (18:45 and 20:00 UTC, after the 18:00 edition job) reads the newest
Daily News edition aloud with Kokoro (`podcasts.speech`, voice `af_heart`): the date, then each
section's headlines and summaries with pauses between them, about 3 minutes in all. It's
encoded to a 64 kbit/s MP3 and added to the `daily-news` feed,
`https://<PUBLIC_HOST>:8445/podcasts/daily-news/feed.xml`, which keeps the newest
14 editions. An edition already read is skipped, so the second run only catches a late
edition. Speaking takes a minute or two of CPU at nice 19. The model (about 350 MB) is
downloaded on first use to `~/.local/share/everythingllm/podcasts/models/kokoro`.

`daily-news` is a feed made on this server, kept in `~/.local/share/everythingllm/podcasts/local.json` rather
than `feeds.json`, so the sync never sees it; `list_podcasts` and the index show it, and
`remove_podcast` refuses it.
Run it by hand:

    systemctl --user start news-audio.service

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
  the host like `make sites-build`. It ends with "Based on reporting by …", linking the
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
`~/.local/share/everythingllm/venvs/research`), shows the runner's progress in the chat, and
replies with what the runner says to tell the user. They talk over a Unix socket the
container sees, `storage/research/runner.sock` (see "Services on the host"):
`start` returns a run id, `wait(run_id, since)` long-polls up to 45 s for new
progress lines and the result, `runs` lists what the runner holds.

A run, step by step:

1. **Plan** — the planner model splits the question into sub-questions with search queries.
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
   as the `sites` server does; the chat
   gets a summary, the link and the sources as citations. If publishing fails (the site
   doesn't keep an entry it couldn't build), the reply gives the saved file and the error.
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

A run doesn't stop when its chat closes, or when AnythingLLM restarts. AnythingLLM aborts
the agent session whenever the chat's websocket closes (the Stop button, a closed tab, a
thread switch and a sleeping phone all look the same to the server); the skill then stops
waiting, and the run carries on in the runner, publishes and embeds the report as usual.
A run nobody was waiting on when it finished gets `chat_closed: true` in the run log. To
find the report, ask in that workspace or open the research site. A run can't be
cancelled from the chat: `make research-setup FORCE=1` restarts the runner, which kills
every run in it. Runs are bounded by their search budget either way. At most 2 run at once;
another waits its turn, and its progress says so.

Only a restart of `research-runner` kills a run without a result, so while a run is going
it has a marker in `~/.local/share/everythingllm/research/runs/running/<id>.json` (its question and when it
started), touched every minute. When the runner starts, it moves every marker into the
log as status `interrupted`, since none of them can be its own; until then, a marker quiet
for its `stale_ms` (3 minutes) reads as interrupted to the audit, and a fresh one as
`running`. The audit's `research_run(question=...)` finds a run by words from its question,
so the agent can tell the user what happened instead of finding no such run.
`make research-setup` and `make units` (when the unit changed) list the live runs and ask
before restarting the runner; with no terminal to ask they stop, unless `FORCE=1`
(`tools/research_guard.py`). `make restart` and `make deploy` restart AnythingLLM only,
so they don't need to ask. The runner runs the code it started with: after changing
`packages/research`, `make research-setup` puts it live.

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
| `POST /runs` | `{"workspace", "thread", "message", "mode", "clientId"}` starts a run: 201 with the run. A `clientId` already used answers 200 with that run and starts nothing; a thread with a running run answers 409. |
| `GET /runs?status=running` | runs with that status (`running`, `done`, `failed`, `cancelled`), oldest first; every kept run without `status` |
| `GET /runs/{id}` | the run (`id`, `clientId`, `workspace`, `thread`, `mode`, `status`, `createdAt`, `finishedAt`); 404 when unknown or expired |
| `GET /runs/{id}/events` | server-sent events: `text` `{"text"}` per piece, then one of `done` `{"citations"}`, `failed` `{"error"}`, `cancelled` `{}`, and the stream closes. Ids count from 1; `Last-Event-ID: n` starts after n. `: ping` every 15 s while live. Any number of followers. |
| `POST /runs/{id}/cancel` | closes the upstream connection and ends the run `cancelled`; a run that has ended is left as it is |
| `/api/v1/...` (any method) | AnythingLLM's developer API, through the relay with its key |
| `GET /health` | 200, no token |

Runs and their events are in SQLite (`~/.local/share/everythingllm/relay/relay.db`, mode 600),
written as each event arrives. A restart fails the runs it cut short with "The relay
restarted during the answer." and keeps their events; finished runs are deleted after 7
days (`RUN_RETENTION_DAYS`). With `NTFY_URL` set, a finished or failed run posts "Answer
ready" or "Answer failed" to that ntfy topic, with the question's first 120 characters
and `run=…,workspace=…,thread=…` as its tags; never the answer.

The secrets live in `~/.config/everythingllm/relay.env` (mode 600), outside the repo, which the
AnythingLLM container mounts: `ANYTHINGLLM_API_KEY` (a developer API key), `RELAY_TOKEN`,
and optionally `NTFY_URL` and `NTFY_TOKEN`. `make relay-setup` makes the file with a fresh
token, refuses to go on until the API key is filled in, then maps the tailnet port and
starts the unit; `make relay-logs` follows it (the `%-logs` rule). `relay.app`'s docstring lists the rest of the
config. Neither the key nor the token appears in a response or a log line, and a test holds
that.

Where it differs from the original spec: `mode` defaults to `chat` when it's left out; a
connection to AnythingLLM that breaks mid-answer, or ten silent minutes, fails the run
("The connection to AnythingLLM broke during the answer.") rather than completing it with
what came; and a `clientId` is remembered as long as its run is kept, so reusing it later
returns that old run.

## System audit

The "System Audit" scheduled job checks this setup every day and publishes what it finds
to the `status` site. The checks and the report are fixed code in the `audit` MCP server,
which forwards each tool call to `audit-runner` on the host (`packages/audit/src/audit/tools.py`,
`host/systemd/audit-runner.service`, socket `storage/audit/runner.sock`), where the checks
can read the journal and every service's socket; the model only writes a summary and
suggests a fix per finding. Its tools:

- `run_checks(since_hours)` — every check, numbered findings grouped as fail / warn / info.
  Fail and warn come in full; info is trimmed to the 5 a report keeps (taken an area, then
  a title, at a time), one line each:
  - logs: error-like lines from AnythingLLM, SearXNG, the pages Caddy,
    and the host services (`WATCHED` in `audit/services.py`) in the host journal, grouped with counts (SearXNG's per-engine errors become
    counts per engine; known noise is skipped, see `NOISE` in `checks.py`);
  - search: a test query to SearXNG, and which engines refuse it;
  - services: every host service in `RUNNERS` (`audit/services.py`) answers `ping` on its
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
- `publish_report(summary, suggestions, status?)` — writes the day's report to
  `status/reports/YYYY-MM-DD` (the Stockholm date) through the sites store and build: the
  findings from this run's `run_checks` (or a fresh run when there's none from the last
  hour), the model's summary, and its
  suggestions keyed by finding number. The `reports` section is `agent_readonly`, so only
  this tool writes it.
- `journal_lines`, `job_run`, `research_run` — the raw material behind a finding
  (`research_run` gives the run's last 30 progress lines).
- `run_job(name)` — runs an existing scheduled job now (AnythingLLM's
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
local API:

    curl -X POST localhost:3001/api/system/update-env -H 'Content-Type: application/json' \
      -d '{"AgentSearXNGApiUrl":"https://<PUBLIC_HOST>:8888/search"}'
    curl -X POST localhost:3001/api/admin/system-preferences -H 'Content-Type: application/json' \
      -d '{"agent_search_provider":"searxng-engine"}'

Some engines block servers now and then (DuckDuckGo answers 403 and is suspended for a
few minutes); the response's `unresponsive_engines` lists them. Check with

    curl -s 'http://127.0.0.1:8888/search?q=test&format=json' | jq '.results | length, .unresponsive_engines'
    journalctl --user -u searxng -f
