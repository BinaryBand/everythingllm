# TODO

Work that's been looked into but not done yet. Remove an entry when it lands.

## Make the test suite faster

`uv run hostctl test` takes about 50 s: `pytest` for 837 tests, plus 0.4 s for the skill
tests. Measured on 2026-10-07, by package:

| Package  | Time  | Where it goes                                                      |
|----------|-------|--------------------------------------------------------------------|
| podcasts | ~11 s | the real sync worker's test takes 2.6 s (`test_start_sync_reaches_the_real_worker`, its 2 s poll), plus real audio work |
| splice   | ~10 s | mostly teardown: 0.5 s per `test_splice_web` test                  |
| sites    | ~7 s  | real zola builds and the article-web tests (`test_web_answers_only_loopback_and_its_own_address` alone is 1.6 s) |
| egress   | ~4 s  | the tunnel idle tests wait out a 0.3 s limit, 0.8 s each           |
| others   | <4 s each |                                                                |

- **splice teardown.** `httpd.shutdown()` waits for `serve_forever()`'s next poll, which
  runs every 0.5 s by default. Pass `poll_interval=0.05` in the fixture
  (`packages/splice/tests/test_splice_web.py:45`). That should save about 6.5 s.
- **podcasts.** Look at the real sync worker's test in `test_library.py` (it waits for
  the worker's next look at the queue) and the audio tests. There might be 3–4 s to cut.
- **Parallel runs with pytest-xdist** (`-n auto`, 4 cores here): this might bring the suite
  to roughly 15 s. First look for timing-sensitive tests that flake under load, and check
  that no two tests share a fixed port or path.

Tests are a small part of how long sessions take. Run one package's tests while
iterating (1–3 s), and the full suite before committing.

## Finish the service-container rollout

The relay, research-runner, sites-runner and the podcasts' runner and workers went live in
their containers on 2026-10-07 (see the proposal's status and the README's "Service
containers"). The checks a script couldn't make, and the cleanup once they've held:

- **Checks that need a person.**
  - Run one Nilson chat to the end through the relay, with its ntfy notice.
  - In Claude Code, reconnect the gateway (`/mcp`). tools/list should show `agents_*`,
    `research_*`, `sandbox_*` and the write tools. `sandbox_run` should write under
    `~/.local/share/everythingllm/sandbox/workspaces/client-claude-code/threads/gateway`.
  - Ask the agent for one Daily News article, which drives the article writer in
    sites-runner's container.
  - Watch the first sync and transcription pass finish in the workers' containers
    (`podcasts/sync.log`, `uv run hostctl podcasts-logs`, `list_podcasts`). The first sync
    started at 00:28 on 2026-10-07.
- **Restart agents-runner** with no delegation going (`uv run hostctl agents-setup`). It has
  run since before the merge, so its live cards still use the old `runs.live`, without the
  check that refuses peers other than loopback and its own address. As a host unit on
  127.0.0.1 it isn't exposed meanwhile.
- **Once the containers have run for a week:** delete the old host venvs
  `~/.local/share/everythingllm/venvs/{relay,research,sites,podcasts}`, and the leftover
  `browser-net` network (`podman network rm browser-net`).
- **Rolling a container back is partly by hand.** `hostctl.units.retired()` only looks in
  `~/.config/systemd/user`, so going back to a host unit means moving
  `~/.config/containers/systemd/<x>.container` aside yourself, then restoring the unit from
  `~/.local/share/everythingllm/backups/`. A `uv run hostctl units --host <app>` could do
  both.

## Gateway loose ends

Left open by the gateway's stages 1–3 (2026-10-06):

- A call outside a client's grant comes back as a JSON-RPC `-32602` error, not as a tool
  result with `isError`. A client shows it as a protocol failure, not as a refusal it can
  read. Decide which is wanted, and test it with Claude Code.
- Research runs aren't per client: `research_wait` and `research_runs` see every run the
  runner holds, AnythingLLM's included. That's documented, not enforced.
- `sandbox_run`'s second wait never happens against the real runner: one wait is 45 s, and
  two don't fit in the caller's 55 s. Remove the loop, or shorten the waits so it can run.

## Format hostctl's older modules

`cli.py`, `appctl.py` and `machine.py` in `packages/hostctl` aren't ruff-formatted, and
`cli.py` has two ruff findings (`PLW1510`, `SIM905`). So `ruff format` on any change there
reformats unrelated code, and the churn has to be undone by hand. Format them once, alone,
in a commit of their own.

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
