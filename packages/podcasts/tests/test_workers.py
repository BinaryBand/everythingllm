"""The queue the runner asks the workers through, their schedules and heartbeats, and the
sync worker's and transcription worker's loops around them (podcasts.worker, podcasts.sync,
podcasts.transcripts)."""

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
from podcasts import sync, transcripts
from podcasts.library import Library
from podcasts.sync import SyncWorker
from podcasts.transcripts import keep_transcribing
from podcasts.worker import (
    ALL_FEEDS,
    RESUMES,
    SYNC_WORKER,
    TRANSCRIBE_WORKER,
    Every,
    Queue,
    heartbeat,
)
from test_library import FEED_URL, Remote, feed


@pytest.fixture
def queue(tmp_path):
    return Queue(tmp_path / "state")


def aged(queue: Queue, name: str, seconds: float) -> None:
    """Make the request `name` look `seconds` old, so the order is certain."""
    when = time.time() - seconds
    os.utime(queue.folder / f"{name}.json", (when, when))


def test_the_longest_waiting_sync_goes_first_and_asking_twice_is_once(queue):
    for slug, age in (("b-show", 10), ("a-show", 20), ("b-show", 0)):
        queue.ask_sync(slug)
        aged(queue, f"sync-{slug}", age)
    assert queue.syncs() == ["a-show", "b-show"]
    assert [queue.take_sync(), queue.take_sync(), queue.take_sync()] == [
        "a-show",
        "b-show",
        None,
    ]


def test_every_feeds_sync_takes_the_single_feeds_with_it(queue):
    queue.ask_sync("a-show")
    aged(queue, "sync-a-show", 10)
    queue.ask_sync(ALL_FEEDS)
    assert queue.take_sync() == ALL_FEEDS
    assert queue.syncs() == []


@pytest.mark.parametrize("target", ["../x", "Show", "", "a/b"])
def test_only_slugs_are_asked_for(queue, target):
    with pytest.raises(ValueError):
        queue.ask_sync(target)


def test_a_heartbeat_goes_stale(queue):
    assert not queue.alive(SYNC_WORKER)
    with heartbeat(queue, SYNC_WORKER, every=0.01):
        time.sleep(0.05)
        assert queue.alive(SYNC_WORKER)
    assert queue.alive(SYNC_WORKER)  # a restart goes unnoticed
    assert not queue.alive(SYNC_WORKER, stale=0)


def at(text: str, day: int = 6) -> datetime:
    """That time on 6 October 2026 (or `day`), local."""
    return datetime.fromisoformat(f"2026-10-{day:02}T{text}").astimezone()


def test_every_comes_due_once_a_slot_like_its_timer(tmp_path):
    every = Every(tmp_path / "x.last", 6, 30)
    wall = every.wall
    assert every.slot(at("06:10")) == wall(at("00:30"))
    assert every.slot(at("06:30")) == wall(at("06:30"))
    assert every.slot(at("00:10")) == wall(at("18:30", day=5))
    # First start: the next slot, not now.
    assert not every.due(at("05:00"))
    assert not every.due(at("06:29"))
    assert every.due(at("06:31"))
    assert not every.due(at("06:45"))
    assert not every.due(at("12:29"))
    assert every.due(at("12:30"))


def test_every_runs_a_slot_missed_while_down_at_once(tmp_path):
    every = Every(tmp_path / "x.last", 6)
    every.ran(at("01:00"))
    # Stopped from 05:00 to 13:00: 06:00 and 12:00 passed, and come due once.
    again = Every(tmp_path / "x.last", 6)
    assert again.due(at("13:00")) and not again.due(at("13:01"))
    (tmp_path / "x.last").write_text("not a time")
    assert not again.due(at("13:02"))  # read as a first start


def test_every_runs_a_slot_once_when_the_clocks_go_back(tmp_path):
    """25 October 2026 in Stockholm: 03:00 CEST becomes 02:00 CET. The 00:00 sync ran
    at 22:00 UTC; at 02:30 CET the latest slot is still that one, not 23:00 UTC."""
    stockholm = ZoneInfo("Europe/Stockholm")
    every = Every(tmp_path / "x.last", 6, tz=stockholm)
    every.ran(datetime(2026, 10, 25, 0, 0, 5, tzinfo=stockholm))
    after = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)  # 02:30 CET
    assert after.astimezone(stockholm).utcoffset() == timedelta(hours=1)
    assert not every.due(after)
    assert every.due(datetime(2026, 10, 25, 6, 0, 1, tzinfo=stockholm))
    # And when they go forward (29 March 2026, 02:00 CET becomes 03:00 CEST), 06:00 still
    # comes once.
    every.ran(datetime(2026, 3, 29, 0, 0, 5, tzinfo=stockholm))
    assert not every.due(datetime(2026, 3, 29, 3, 30, tzinfo=stockholm))
    assert every.due(datetime(2026, 3, 29, 6, 0, 1, tzinfo=stockholm))
    assert not every.due(datetime(2026, 3, 29, 6, 30, tzinfo=stockholm))


class Worker(SyncWorker):
    """The sync worker with the fake internet, stopping once the queue is empty."""

    def __init__(self, lib: Library, remote: Remote):
        super().__init__(
            lib.state, library=lambda: lib, client=remote.client, log=self.heard
        )
        self.lines: list[str] = []

    def heard(self, line: str) -> None:
        self.lines.append(line)

    def drain(self) -> None:
        while self.step():
            pass


def subscribed(lib: Library, remote: Remote) -> str:
    with remote.client() as client:
        return lib.add(client, FEED_URL, keep=2)[0]


def test_the_worker_runs_what_was_asked_with_its_output_in_sync_log(lib, capsys):
    remote = Remote(feed(("a", "1"), ("b", "2")))
    slug = subscribed(lib, remote)
    w = Worker(lib, remote)
    w.every.ran(datetime.now().astimezone())  # not due: only what is asked for runs
    lib.queue.ask_sync(slug)
    w.drain()
    assert lib.queue.syncs() == []
    assert [e.guid for e in lib.record(slug)["show"].episodes] == ["b", "a"]
    assert lib.last_sync()["finished"] and not lib.last_sync().get("stopped")
    log = (lib.state / "sync.log").read_text()
    assert "syncing the-show" in log
    assert w.lines == ["syncing the-show", "synced the-show"]
    assert "syncing the-show" not in capsys.readouterr().out  # the journal has its own


def test_the_worker_asks_for_every_feed_when_its_slot_comes(lib):
    remote = Remote(feed(("a", "1")))
    subscribed(lib, remote)
    w = Worker(lib, remote)
    w.every.ran(at("00:00", day=1))  # days ago
    w.drain()
    assert w.lines == ["syncing every feed", "synced every feed"]
    assert lib.record("the-show")["show"].episodes


def test_a_sync_that_finds_the_lock_held_is_asked_for_again(lib):
    w = Worker(lib, Remote(feed(("a", "1"))))
    w.every.ran(datetime.now().astimezone())
    built = []
    w.library = lambda: built.append(lib) or lib
    lib.queue.ask_sync(ALL_FEEDS, tries=1)  # it died with a worker once before
    with lib._lock("sync.lock"):  # a transcript being saved
        assert not w.step()
        said = (lib.state / "sync.log").read_text()
        # While it's held, the worker looks again without taking the request, saying so
        # again or building a Library (an old sync can hold it for hours).
        for _ in range(3):
            assert not w.step()
        assert (lib.state / "sync.log").read_text() == said
        assert w.lines == ["syncing every feed", "sync.lock is held; waiting"]
        assert len(built) == 1
        # The request keeps its tries, so the guard against crash loops still holds.
        assert lib.queue.tries(f"sync-{ALL_FEEDS}") == 1
        assert lib.queue.held() is None
    assert lib.queue.syncs() == [ALL_FEEDS]
    assert "another sync is running" in said
    assert w.step()  # free: it runs, once
    assert not w.step()
    assert len(built) == 2 and w.lines[-1] == "synced every feed"
    assert lib.queue.syncs() == [] and lib.last_sync()["finished"]


class Waits(threading.Event):
    """A stop event that notes how long the worker waits, and is set after `times`."""

    def __init__(self, times: int):
        super().__init__()
        self.times, self.waited = times, []

    def wait(self, timeout: float | None = None) -> bool:
        self.waited.append(timeout)
        if len(self.waited) >= self.times:
            self.set()
        return self.is_set()


def test_a_worker_waiting_for_the_lock_looks_at_it_less_often(lib):
    w = Worker(lib, Remote(feed(("a", "1"))))
    w.every.ran(datetime.now().astimezone())
    w.stop = Waits(2)
    w.run()
    assert w.stop.waited == [sync.POLL_SECONDS] * 2  # idle
    lib.queue.ask_sync(ALL_FEEDS)
    w.stop = Waits(3)
    with lib._lock("sync.lock"):
        w.run()
    assert w.stop.waited == [sync.BLOCKED_SECONDS] * 3
    assert w.lines.count("sync.lock is held; waiting") == 1


def test_a_crash_is_recorded_and_the_worker_goes_on(lib):
    remote = Remote(feed(("a", "1")))
    subscribed(lib, remote)
    w = Worker(lib, remote)
    w.every.ran(datetime.now().astimezone())

    def broken() -> None:
        raise RuntimeError("no index today")

    lib.write_index = broken
    lib.queue.ask_sync(ALL_FEEDS)
    w.drain()
    assert "RuntimeError: no index today" in lib.last_sync()["error"]
    assert "Traceback" in (lib.state / "sync.log").read_text()
    assert not lib.sync_running()
    del lib.write_index
    lib.queue.ask_sync(ALL_FEEDS)
    w.drain()
    assert lib.last_sync()["error"] == ""


def test_a_stop_ends_the_sync_mid_download_and_asks_for_it_again(lib):
    remote = Remote(feed(("a", "1"), ("b", "2"), ("c", "3")))
    slug = subscribed(lib, remote)
    w = Worker(lib, remote)
    w.every.ran(datetime.now().astimezone())
    gets = remote.handler

    def stop_after_one_download(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".mp3"):
            w.stop.set()  # SIGTERM, mid-download
        return gets(request)

    remote.handler = stop_after_one_download
    lib.queue.ask_sync(slug)
    w.step()
    # c's download is cut off where it was (the worker's stop timeout is short), and it
    # waits for the next sync, as b and a do.
    assert lib.record(slug)["show"].episodes == []
    assert not list(lib.audio.dir.glob(".dl-*"))  # nor a half-written download
    assert lib.last_sync()["stopped"] and lib.last_sync()["finished"]
    assert lib.queue.syncs() == [slug]  # for the next start
    assert lib.queue.held() is None
    assert w.lines[-1] == "synced the-show (stopped part-way)"


def test_a_sync_the_worker_didnt_live_through_is_asked_for_again(lib):
    """Killed after its stop timeout, out of memory, or a crash before the sync's own
    try: its request was taken, but it's held until done, and the next start asks again."""
    remote = Remote(feed(("a", "1")))
    slug = subscribed(lib, remote)
    w = Worker(lib, remote)
    w.every.ran(datetime.now().astimezone())
    lib.queue.ask_sync(slug)

    def dies() -> Library:
        raise MemoryError

    w.library = dies
    with pytest.raises(MemoryError):
        w.step()
    assert lib.queue.syncs() == [] and lib.queue.held() == (slug, 0)
    again = Worker(lib, remote)
    again.resume()
    assert lib.queue.syncs() == [slug] and lib.queue.held() is None
    assert "didn't finish" in again.lines[-1]
    again.drain()
    assert lib.record(slug)["show"].episodes and lib.queue.held() is None


def test_a_sync_that_kills_the_worker_every_time_waits_for_the_schedule(lib):
    w = Worker(lib, Remote(feed(("a", "1"))))
    w.every.ran(datetime.now().astimezone())

    def dies() -> Library:
        raise MemoryError

    w.library = dies
    lib.queue.ask_sync(ALL_FEEDS)
    for _ in range(RESUMES):
        with pytest.raises(MemoryError):
            w.step()
        w.resume()
        assert lib.queue.syncs() == [ALL_FEEDS]
    with pytest.raises(MemoryError):
        w.step()
    w.resume()
    assert lib.queue.syncs() == [] and lib.queue.held() is None
    assert "waits for the next scheduled sync" in w.lines[-1]
    # A sync asked for anew starts counting again.
    lib.queue.ask_sync(ALL_FEEDS)
    assert lib.queue.take_sync() == ALL_FEEDS and lib.queue.held() == (ALL_FEEDS, 0)


def test_the_worker_loop_stops_when_told(lib):
    w = Worker(lib, Remote(feed(("a", "1"))))
    loop = threading.Thread(target=w.run)
    loop.start()
    for _ in range(100):
        if lib.queue.alive(SYNC_WORKER):
            break
        time.sleep(0.01)
    w.stop.set()
    loop.join(timeout=5)
    assert not loop.is_alive()


def test_the_transcription_worker_passes_when_due_asked_or_left_unfinished(tmp_path):
    state = tmp_path / "state"
    queue = Queue(state)
    passes, stop = [], threading.Event()
    every = Every(queue.folder / "transcribe-worker.last", 6, 30)
    every.ran(at("00:00", day=1))  # a slot was missed
    queue.ask("transcribe")
    threading.Timer(0.3, stop.set).start()
    keep_transcribing(state, lambda: passes.append("pass"), stop, poll=0.01)
    # Due and asked at once: one pass for both, then nothing until a slot or a request.
    assert passes == ["pass"]
    assert not queue.take("transcribe")


def test_a_pass_left_unfinished_runs_as_the_worker_starts(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "transcribing.json").write_text("{}")  # the last worker died in a pass
    passes, stop = [], threading.Event()

    def run_pass() -> None:
        passes.append("pass")
        Queue(state).ask("transcribe")  # asked for meanwhile: one more
        if len(passes) == 2:
            stop.set()

    keep_transcribing(state, run_pass, stop, poll=0.01)
    assert passes == ["pass", "pass"]
    assert Queue(state).alive("transcribe-worker")


def test_the_threads_come_from_host_env_at_every_pass(tmp_path, monkeypatch):
    host_env = tmp_path / "host.env"
    monkeypatch.setattr(transcripts, "HOST_ENV", host_env)
    monkeypatch.setenv("PODCASTS_TRANSCRIBE_THREADS", "2")
    assert transcripts.threads_setting() == "2"  # no host.env: as it started
    host_env.write_text("PODCASTS_TRANSCRIBE_THREADS='08:00=4,22:00=1'\n")
    assert transcripts.threads_setting() == "08:00=4,22:00=1"


def test_a_bad_threads_setting_skips_the_pass(lib, tmp_path, monkeypatch):
    monkeypatch.setenv("PODCASTS_STATE", str(lib.state))
    monkeypatch.setenv("PODCASTS_DIR", str(lib.site))
    monkeypatch.setattr(transcripts, "HOST_ENV", tmp_path / "host.env")
    monkeypatch.setenv("PODCASTS_TRANSCRIBE_THREADS", "lots")
    heard = []
    transcripts.transcribe_pass(heard.append)
    assert "no pass until it's fixed" in heard[0]
    with lib.only_one("transcribe.lock"):
        transcripts.transcribe_pass(heard.append)
    assert heard[-1] == "another transcription run is going"


def test_the_scripts_ask_by_hand(lib, monkeypatch, capsys):
    monkeypatch.setenv("PODCASTS_STATE", str(lib.state))
    monkeypatch.setenv("PODCASTS_DIR", str(lib.site))
    lib.queue.beat(SYNC_WORKER)
    monkeypatch.setattr("sys.argv", ["podcasts-sync"])
    sync.ask()
    assert lib.queue.syncs() == [ALL_FEEDS]
    assert "asked for a sync of every feed" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["podcasts-sync", "nope"])
    with pytest.raises(SystemExit, match="no podcast named 'nope'"):
        sync.ask()
    with pytest.raises(SystemExit, match="the worker isn't running"):
        transcripts.ask()
    assert lib.queue.take("transcribe")


def test_the_transcription_worker_exits_143_on_sigterm(lib):
    """As the unit runs it: SuccessExitStatus=143 counts the stop as no failure."""
    env = {
        **os.environ,
        "PODCASTS_STATE": str(lib.state),
        "PODCASTS_DIR": str(lib.site),
    }
    worker = subprocess.Popen(
        [sys.executable, "-m", "podcasts.transcripts"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        for _ in range(150):
            if lib.queue.alive(TRANSCRIBE_WORKER):
                break
            time.sleep(0.1)
    finally:
        worker.send_signal(signal.SIGTERM)
        out, _ = worker.communicate(timeout=30)
    assert worker.returncode == 143, out
    assert "transcribing every 6 hours at :30" in out
