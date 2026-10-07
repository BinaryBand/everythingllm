# TODO

Work that's been looked into but not done yet. Remove an entry when it lands.

## Make the test suite faster

`uv run hostctl test` takes about 55 s: `pytest` for 785 tests, plus 0.4 s for the skill
tests. Measured on 2026-10-07, by package:

| Package  | Time  | Where it goes                                                      |
|----------|-------|--------------------------------------------------------------------|
| sites    | ~7 s  | real zola builds and the article-web tests (`test_web_answers_only_loopback_and_its_own_address` alone is 1.6 s) |
| egress   | ~4 s  | the tunnel idle tests wait out a 0.3 s limit, 0.8 s each           |
| others   | <4 s each |                                                                |

- **Parallel runs with pytest-xdist** (`-n auto`, 4 cores here): this might bring the suite
  to roughly 15 s. First look for timing-sensitive tests that flake under load, and check
  that no two tests share a fixed port or path.

Tests are a small part of how long sessions take. Run one package's tests while
iterating (1–3 s), and the full suite before committing.

## Finish the service-container rollout

The relay, research-runner and sites-runner went live in their containers on 2026-10-07 (see the proposal's status and the README's "Service
containers"). The checks a script couldn't make, and the cleanup once they've held:

- **Checks that need a person.**
  - Run one Nilson chat to the end through the relay, with its ntfy notice.
  - Ask the agent for one Daily News article, which drives the article writer in
    sites-runner's container.
- **Once the containers have run for a week:** delete the old host venvs
  `~/.local/share/everythingllm/venvs/{relay,research,sites}`, and the leftover
  `browser-net` network (`podman network rm browser-net`).
- **Rolling a container back is partly by hand.** `hostctl.units.retired()` only looks in
  `~/.config/systemd/user`, so going back to a host unit means moving
  `~/.config/containers/systemd/<x>.container` aside yourself, then restoring its template
  to `host/systemd/` from git and running `uv run hostctl units`. A `uv run hostctl units --host <app>` could do
  both.

## Gateway loose ends

Left open by the gateway's stages 1–3 (2026-10-06):

- Research runs aren't per client: `research_wait` and `research_runs` see every run the
  runner holds, AnythingLLM's included. That's documented, not enforced, and matters only
  once a second client has the `research` grant.

## Saved logins in the browser: loose ends

Left open by the code review of the browser's saved logins (2026-10-07):

- **The view's poll does more than it needs.** `Takeover.state()` (`takeover.py`) runs
  every 2 s per open view and each time decrypts the vault and asks the driver for its
  offers, even when nothing is being captured. Cheap today; caching on the vault file's
  mtime and returning offers only while one is pending would cut it.
- **One approval at a time per workspace.** A `Session` holds one `approval`
  (`runner.py`), so when two chats in a workspace (or a scheduled job and a chat) ask for
  ask-first logins at once, each request makes the other stale. Both `browser-login`
  calls are told to call again, they keep replacing each other, and the user only sees
  the last one. Fixing it means keeping the waiting approvals by id and having the
  take-over view list each one with its own allow and refuse buttons.

## Format hostctl's older modules

`cli.py`, `appctl.py` and `machine.py` in `packages/hostctl` aren't ruff-formatted, and
`cli.py` has two ruff findings (`PLW1510`, `SIM905`). So `ruff format` on any change there
reformats unrelated code, and the churn has to be undone by hand. Format them once, alone,
in a commit of their own.

## Gaps the Muse probes showed

From `docs/muse-parity.md` (2026-10-07), after page scripts, chat attachments in `/work` and
the scheduled-job skills. Most of these combine tools the agent already has rather than
adding services.

- **Decide whether `docs/muse-parity.md` stays in the public repo.** It's committed but
  unpushed, and since then it has an uncommitted edit (B1 marked fixed) that names a
  personal Gmail address and the calendar bridge's setup. Either keep a public version
  without those details, or move it to `docs/.proposals/` and drop it from history before
  the next push.
- **Post long jobs back to their chat.** Deep research and delegations end on a live card;
  the chat is never told. One mechanism in `runs` (the library every runner shares) that
  appends a message to the originating thread through the developer API would cover both,
  and any later background work.
- **One `notify` call.** The relay already posts to ntfy. A skill (or a job tool) that
  sends a short notice would carry reminders, job results and check-ins to the phone.
- **Pages that keep state.** Sandboxed pages have an opaque origin, so no `localStorage`:
  a flashcard deck forgets its place. Persistence needs an origin per workspace (a port
  or subdomain each) or a write-back op through the sandbox runner.
- **Activity feed.** A scheduled job that writes a page from the run logs (research,
  agents, scheduled jobs), linked by its card.
- **Memory the agent can see (B6).** AnythingLLM has a native `memories` table the agent
  doesn't know about. A skill to list and forget entries, and a prompt line, rather than a
  new store.
- **The built-in `create-scheduled-job` gets the day wrong** across midnight and DST: its
  local→UTC conversion shifts only the minute and hour. Recurring jobs made with it can
  run on the wrong day. Upstream's to fix; until then the prompt could steer daily
  times that cross midnight UTC through the new skills.
- **Model choice for agent turns.** Several probe failures (a "one-off" stored as yearly,
  not knowing the home city that the prompt states) look like `glm-5.3-flash`'s limits.
  Try a stronger model for agent turns and compare on the probes.
- **The probes as scenario runs.** Run the 11 probes through agents-runner on demand and
  keep their tool calls and results, so each change is measured against the 2026-10-07
  baseline.

## Push notifications for Nilson through UnifiedPush

The relay can post to ntfy (`NTFY_URL`, `NTFY_TOKEN` in `relay.env`) when an answer is ready
or fails. Push to Nilson itself would make those two settings unnecessary.

- **Why not AnythingLLM's push.** AnythingLLM's `storage/push-notifications/vapid-keys.json`
  is browser Web Push. It reaches only a browser that subscribed through AnythingLLM's web UI
  and its service worker. In single-user mode that subscription would be
  `primary-subscription.json`, and no browser has subscribed. Nilson is a native Flutter
  app, so it can't receive this.
- **What would.** UnifiedPush. Nilson registers with a distributor (on Android, the ntfy
  app or an FCM-backed one; on Linux, a D-Bus distributor such as KUnifiedPush) and sends
  the relay its endpoint. The relay then sends encrypted Web Push (RFC 8291) there, signed
  with VAPID.
- **Relay side.**
  - A VAPID key pair of the relay's own, made on first start in
    `~/.local/share/everythingllm/relay/`, not AnythingLLM's.
  - A `POST /push` route (bearer token, as for the other routes) that keeps the endpoint
    in `relay.db`.
  - `pywebpush`.
  - The same "Answer ready" and "Answer failed" payloads as now, never the answer.
  - Remove `NTFY_*` from `relay.env`, `relay_env.py` and the README.
- **Nilson side.** The `unifiedpush` Flutter package, plus a distributor installed on each
  device.
