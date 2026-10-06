# TODO

Work that's been looked into but not done yet. Remove an entry when it lands.

## Make the test suite faster

`make test` takes about 33 s: `pytest` for 444 tests, about 4 s of it collection, plus
0.6 s for the skill tests. Measured on 2026-10-06, by package:

| Package  | Time  | Where it goes                                                      |
|----------|-------|--------------------------------------------------------------------|
| podcasts | ~12 s | ~8 s in test bodies: two sync tests take 1 s each (`test_start_sync_*`), plus real audio work |
| splice   | ~10 s | ~7 s in teardown: 0.5 s per `test_splice_web` test                 |
| sites    | ~7 s  | real zola builds and the article-web tests                         |
| others   | <4 s each |                                                                |

- **splice teardown.** `httpd.shutdown()` waits for `serve_forever()`'s next poll, which
  runs every 0.5 s by default. Pass `poll_interval=0.05` in the fixture
  (`packages/splice/tests/test_splice_web.py:45`). That should save about 6.5 s.
- **podcasts.** Look at the 1 s waits in `test_library.py`'s sync tests and the audio
  tests. There might be 3–4 s to cut.
- **Parallel runs with pytest-xdist** (`-n auto`, 4 cores here): this might bring the suite
  to roughly 10–12 s. First look for timing-sensitive tests that flake under load, and check
  that no two tests share a fixed port or path.

Tests are a small part of how long sessions take. Run one package's tests while
iterating (1–3 s), and the full suite before committing.

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
