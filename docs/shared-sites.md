# Plan: per-workspace sharing and publishing, shared themes, and sites the agent designs

Status: stages 0 and 1 done 2026-10-06 (per-workspace `shared/`, `/system/themes`, and
`public/` synced to the pages site); stages 2 and 3 not started. Replaces an earlier draft
built on one global `/shared` with a manifest of write zones (see "Rejected").

## Goal

- Workspaces can share what they make, such as themes, templates and data, without any
  workspace being able to change another's.
- Each workspace has a folder whose contents are its pages on the web: put a page there
  and it goes live, remove it and it comes down.
- The agent can design and build Zola sites: its own sites from a workspace's `/project`,
  and later the look of the system sites (news, research, status).
- Builds of anything the agent wrote happen in the sandbox, never with the host's zola.
- Read-only is the default everywhere: the only folders a run can write are its own.

## The model

| In a run of workspace A | Host folder                                   | A can     | Others can |
|-------------------------|-----------------------------------------------|-----------|------------|
| `/work`                 | `sandbox/workspaces/A/threads/<thread>/`      | write     | nothing    |
| `/project`              | `sandbox/workspaces/A/project/`               | write     | nothing    |
| `/shared/A/`            | `sandbox/workspaces/A/shared/`                | write     | read       |
| `/shared/B/`, …         | each other workspace's `shared/`              | read      | (theirs)   |
| `/public`               | `sandbox/workspaces/A/public/`                | write     | the web    |
| `/system/themes/`       | the repo's `zola/themes/`                     | read      | read       |

(Host folders are under `~/.local/share/everythingllm/`.)

Three words, three audiences: `project` is the workspace's own, `shared` is for the other
workspaces, and `public` is for the web.

- **Every workspace's `shared/` is visible to every other workspace, read-only.** That's
  the default, enforced by the kernel through read-only binds. What should stay private
  goes in `/project`.
- **What it removes.** Nobody writes another workspace's folders, so a prompt injection in
  one chat can't change what other workspaces use. There's no manifest, no grants and no
  new locking.
  - A writes `/shared/A` under A's existing workspace lock.
  - Other workspaces see it read-only, so they can't plant a symlink under a path A's host
    operation is about to resolve.
  - Runs in different workspaces fully overlap.
- **Provenance.** `/shared/career/themes/minimal` was made by Career. A reader knows whose
  content it's trusting.
- **Quota.** `shared/` counts toward its workspace's existing 5 GB.
- **Changing someone else's work.** You don't edit another workspace's theme. You copy it
  into your own `/project` or `/shared` and change the copy.
- **Mount names.** Workspace slugs are already restricted (`KEY_RE`), so `/shared/<slug>`
  is always a safe mount name. `_jobs` gets one like any workspace.
- **`/system`** is the one place everyone reads and nobody in the sandbox writes. It holds
  what the repo provides: the `agent-site` theme today. Only the repo changes it.
- **`/public` replaces the read-only `/pages` mount.** A workspace sees its live pages as
  files it can edit, rather than as a read-only copy.

## Publishing: `public/` synced to the web

A workspace's `public/` is the source of truth for its pages. Caddy doesn't serve it
directly; the runner keeps the served copy in step with it.

- **Mapping.** `public/<slug>/` is served as `https://<host>:8445/<slug>/`, and a single
  `public/<slug>.html` as a one-file page. URLs and the ownership rules are today's: a
  slug belongs to the workspace that published it first, and another workspace's
  `public/<same-slug>` is refused at sync with a note saying so.
- **Automatic sync.** When a run, a `write-file` or a `build-site` ends having changed
  `public/`, the runner syncs that workspace's pages. The end of an operation is a
  consistent point, so pages go live whole, never mid-write, a moment after the change.
  The operation's reply lists what went live, what came down and any CSP warnings.
- **Removal.** A page owned by the workspace whose folder or file is gone from `public/`
  is taken down at the next sync. That's the only way sync removes anything, and only the
  workspace's own pages.
- **`publish`** stays, for forcing a sync of everything or of one slug. With a path
  outside `public/`, it copies that file or folder into `public/<slug>` first, so old
  habits keep working.
- **The safe copy.** Every sync uses `publish`'s existing path:
  - plain files only, never symlinks, FIFOs or devices
  - a size cap per page
  - dotfiles such as `.git` left out
  - the page built beside its destination and swapped in atomically
  - the CSP check, link cards and the index page

  So the served folder only ever holds plain files the runner copied itself.
- **Quota.** `public/` counts toward the workspace's 5 GB like its other folders.

Why not serve `public/` directly: hosts that publish untrusted content (GitHub Pages,
Netlify, Cloudflare Pages) deploy a copy in the same way. The opposite model, a web server
reading users' folders (the old `~user/public_html`), is the classic source of symlink
holes. Doing it here would need:
- a `nosymfollow` mount, which depends on podman passing it through
- a workspace prefix on every URL, so two workspaces can't both have `public/notes`

It would still leave FIFOs and half-written pages served as they are. Automatic sync gives
the same "what's in the folder is what's live" with none of that.

## Tools stay out of the sandbox's reach

zola is already in the sandbox image. The build helper, which assembles a site with its
theme and runs zola, ships from the repo too, inside the image or run by the runner.
Neither lives in a folder the agent can write. A shared build script in `/shared` would let
whoever writes it put code into every workspace's builds. `/shared` and `/system` are
mounted `noexec`, and the system prompt tells the agent never to run code from another
workspace's `/shared`.

## Threats, and what answers each

1. **The host reading sandbox-written files.** The host never parses workspace files.
   - Assembling a site (its folder plus the theme it names) and building it both happen
     inside a build container.
   - The host only copies finished pages out of `public/` through `publish`'s existing
     path: regular files only, no symlinks, a size cap and the CSP check. Caddy serves
     that copy, never a workspace folder.
2. **Agent templates run by the host's zola**, whose `load_data` can read local files.
   Builds of agent-written sites and themes happen only in a container with no network,
   with `/project`, `/shared` and `/system` mounted read-only and an empty output folder.
3. **One workspace changing another's work.** It can't: other workspaces' `shared/`
   folders are mounted read-only, and the runner's file operations only accept the
   caller's own folders.
4. **Being influenced by what another workspace shares.** This one remains, because
   reading is the point of sharing. What limits it:
   - provenance, since the path names the source
   - nothing executable comes from there (`noexec`, and the system prompt)
   - themes only become HTML under the pages site's CSP (no scripts, nothing from other
     hosts)
5. **A system site's look depending on a workspace.** Once a system site uses a
   workspace's theme, that workspace's later edits change the system site at its next
   build. The choice is made in the repo (see stage 3), and Decision 2 covers pinning.

## The build

A runner operation, `build_site(scope, path)`, with a `build-site` skill:

1. **Which site.** `path` is a site folder in the caller's `/project` or its own
   `/shared`, such as `/project/sites/portfolio`.
2. **The container.** It runs from the sandbox image with `--network none`, the usual
   limits and a 40 s timeout. It mounts read-only the caller's `/project`, every
   workspace's `/shared` and `/system`, plus an empty `/out`.
3. **The helper.** Taken from the image, it copies the site to `/tmp` and resolves its
   theme. The site's `zola.toml` names the theme (`theme = "agent-site"`) and where it
   comes from:

   ```toml
   [extra.build]
   theme_from = "system"   # or a workspace slug, e.g. "career", for /shared/career/themes
   ```

   The helper copies that theme into the site's `themes/` and runs
   `zola build --base-url <url>`. The runner passes the URL in, so a site can't point its
   links at another host.
4. **Publishing.** The runner copies `/out` into the caller's `public/<slug>/`, replacing
   what was there, and the automatic sync puts it live. The slug is the site folder's
   name, or one the call gives. So what's in `public/` is always what's live, built sites
   included.

The lab moves to `education/shared/sites/lab` (Decision 1) and is built this way. It stops
being editable by every workspace, since others can copy it.

## System sites (later)

The news, research and status sites keep their entries on the host, in `pages/entries/`.
`SiteStore`, the services and the `sites` tools write them exactly as now, with every
check they have. Only the theme and the build change:

- **Choosing a theme.** A setting in the repo picks each site's theme, for example
  `news.theme = "system:agent-site"` today, or `"education:newsroom"` once a workspace has
  made one. The repo is the only place this choice can be made.
- **Building.** `sites-runner` asks `sandbox-runner` to build the site in a container: the
  site's repo config, its entries mounted read-only, and the chosen theme. The output is
  published with the existing `.zola-site` swap. Repo-only templates keep building as they
  do today until a site is switched.

No host service ever writes into `/shared`, and the agent still changes entries only
through the `sites` tools.

## Stages

Each stage lands on its own and leaves everything working.

0. **Per-workspace `shared/` and `/system/themes`.**
   - Mounts: `/shared/<own>` read-write, every other workspace's read-only, `/system`
     read-only.
   - Remove the global `/shared`, its lock (`_shared`, `idle_any`), `SHARED_OWNER` pages
     and `SHARED_MAX_BYTES`. Move the lab into `education/shared/sites/lab`.
   - Update the skill texts and the system prompt.
   - Tests:
     - a run's mounts
     - writes to another workspace's `/shared` refused, through `write-file` and from
       inside a run (`EROFS`)
     - runs in two workspaces overlapping
     - the quota counting `shared/`
     - `/system` read-only
1. **`public/` and automatic sync.**
   - Each workspace gets `public/`, which replaces the `/pages` mount. Runs, writes and
     builds that change it sync it when they end. `publish` becomes a forced sync, and a
     copy into `public/` for paths outside it.
   - Seed each workspace's `public/` from its pages that are live now, so nothing comes
     down at the first sync.
   - Tests:
     - a page written into `public/` goes live after the run, and not before it ends
     - a removed folder comes down
     - another workspace's page with the same slug is refused, and that page stays
     - symlinks, FIFOs and dotfiles are left out of the copy
     - a page larger than the cap is refused
     - CSP warnings appear in the reply
     - the first sync after seeding changes nothing
2. **`build_site` and the `build-site` skill.**
   - Build from `/project` or the caller's own `/shared`, with the theme resolved from
     `/system` or a workspace's `/shared`. The output goes into the caller's
     `public/<slug>/` and is synced.
   - The lab is built this way.
   - Tests:
     - the container's arguments (no network, mounts, base URL)
     - a missing or unknown theme
     - a failed build publishing nothing
     - a symlink in the output left out
     - the page owned by the caller
3. **System sites built in the sandbox with a chosen theme.**
   - Add the repo setting and the `sandbox-runner` operation for `sites-runner`.
   - Switch `status` first, then `research`, then `news`, each checked live.

## Decisions

1. **Who owns the lab** once it leaves the global `/shared`. `education` is the default
   here.
2. **Pinning a system site's theme.** Either it follows the workspace's theme as it
   changes, or the setting names a snapshot (copied into the repo, or a git commit of the
   theme) so a workspace's later edits don't restyle the news site by surprise.
3. **Gateway clients** (see `proposals/gateway-and-containers.md`). They'd get a workspace
   folder set of their own (`client-<name>`), so they could share through `/shared` too.

## Later: choosing who sees what

Sharing with specific workspaces only ("Career shares this folder with Education") would
be a layer on top of this one. It would be a grant that mounts one folder of A's read-only
into B's runs, possibly made through an MCP tool of its own. It doesn't need these folders
to change.

## Rejected

- **One global `/shared` everyone writes** (what runs today). Any workspace could change
  anything another workspace relies on.
- **One global `/shared` with a manifest of write zones** (the earlier draft of this plan).
  It worked, but it needed:
  - grants for every zone
  - a nesting rule
  - per-zone locks
  - entry operations in the runner, so that services never read `/shared`

  That's a lot of machinery to allow cross-workspace writes this design doesn't need.
- **Signed `.permissions.toml` files in each zone.** That keeps rules in folders the
  sandbox can write, and needs a signing key and pinning mounts to be safe.
- **Build tools in `/shared`.** Whoever writes them puts code into every workspace's
  builds.
- **Caddy serving each workspace's `public/` directly.** See "Publishing" above: it would
  need a `nosymfollow` mount and a workspace prefix on every URL, and would still serve
  FIFOs and half-written pages. Automatic sync gives the same experience safely.
- **Publishing only on an explicit `publish`.** It was safe, but it kept a step between
  "the page is in `public/`" and "the page is live" that the folder model doesn't need.
  Syncing at the end of each operation keeps pages whole without that step.
- **Moving the system sites' entries into the sandbox.** Their writers are host services,
  and keeping entries host-side keeps every check `SiteStore` makes, with no service ever
  reading sandbox files.
