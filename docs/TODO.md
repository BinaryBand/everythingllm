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
  (`src/mcps/splice/tests/test_splice_web.py:45`). That should save about 6.5 s.
- **podcasts.** Look at the 1 s waits in `test_library.py`'s sync tests and the audio
  tests. There might be 3–4 s to cut.
- **Parallel runs with pytest-xdist** (`-n auto`, 4 cores here): this might bring the suite
  to roughly 10–12 s. First loosen the timing-sensitive tests, which flake under load:
  `src/mcps/research/tests/test_research_runner.py::test_a_run_nobody_waits_on_counts_the_chat_as_closed`
  sleeps 0.2 s against a 0.05 s grace and has already failed once in a full run. Also check
  that no two tests share a fixed port or path.

Tests are a small part of how long sessions take. Run one package's tests while
iterating (1–3 s), and the full suite before committing.
