import asyncio
import itertools
import os
import re
import shutil
from pathlib import Path

import hostrpc
import pytest
from sandbox import runner
from sandbox.runner import Config, Runner

A = {"workspace": "career", "thread": "12"}
A2 = {"workspace": "career", "thread": "default"}
B = {"workspace": "home", "thread": "default"}


def mounts(args):
    """{target: source} of a podman run's -v options."""
    return {
        v.split(":")[1]: Path(v.split(":")[0])
        for k, v in itertools.pairwise(args)
        if k == "-v"
    }


class FakePodman:
    """Stands in for podman: records calls, and on `run` does `effect(mounts)` like the container would."""

    def __init__(self, result=(0, "hi\n", "", False), effect=None, oom=False, delay=0):
        self.calls = []
        self.result = result
        self.effect = effect
        self.oom = oom
        self.delay = delay

    async def __call__(self, args, timeout, kill):
        self.calls.append((args, timeout, kill))
        if args[0] == "inspect":
            return 0, "true\n" if self.oom else "false\n", "", False
        if args[0] != "run":
            return 0, "", "", False
        if self.effect:
            self.effect(mounts(args))
        await asyncio.sleep(self.delay)
        return self.result

    def runs(self):
        return [c for c in self.calls if c[0][0] == "run"]


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "site").mkdir()
    (tmp_path / "themes" / "agent-site").mkdir(parents=True)
    return Config(
        socket=tmp_path / "sock" / "runner.sock",
        root=tmp_path / "sandbox",
        system_themes=tmp_path / "themes",
        site_dir=tmp_path / "site",
        site_url="https://pages.example/",
        public_root=tmp_path / "public",
        public_url="https://ws.example/",
    )


def make(cfg, **kw):
    return Runner(cfg, podman=FakePodman(**kw))


def go(coro):
    return asyncio.run(coro)


def work(cfg, scope):
    return cfg.root / scope["workspace"] / "threads" / scope["thread"]


def project(cfg, scope):
    return cfg.root / scope["workspace"] / "project"


def shared(cfg, scope):
    return cfg.root / scope["workspace"] / "shared"


def public(cfg, scope):
    return cfg.public_root / scope["workspace"]


def test_a_run_mounts_its_threads_work_and_its_workspaces_project(cfg):
    seen = {}

    def effect(m):
        seen.update(m)
        seen["script"] = (m["/sandbox"] / "main.py").read_text()

    r = make(cfg, effect=effect)
    res = go(r.op_run(A, "python", "print('hi')"))
    assert res["stdout"] == "hi\n" and res["exit_code"] == 0
    assert seen["/work"] == work(cfg, A) and seen["/project"] == project(cfg, A)
    assert seen["script"] == "print('hi')"
    assert not any(cfg.scripts.iterdir())  # removed after the run
    args, timeout, kill = r.podman.runs()[0]
    assert args[-2:] == ["python", "/sandbox/main.py"]
    assert any(a.endswith(":/sandbox:ro") for a in args)
    assert f"{public(cfg, A)}:/public:rw,noexec,nosuid,nodev" in args
    assert not any("/pages" in a for a in args)
    assert kill == args[args.index("--name") + 1]
    assert timeout == runner.DEFAULT_TIMEOUT
    assert r.podman.calls[-1][0][:2] == ["rm", "-f"]


def test_podman_args_isolate_the_run(cfg):
    r = make(cfg)
    go(r.op_run(A, "bash", "id", timeout=9999))
    args, timeout, _ = r.podman.runs()[0]
    joined = " ".join(args)
    for flag in [
        "--network sandbox-net",
        "--dns none",
        "--read-only",
        "--cap-drop ALL",
        "--pids-limit 256",
        f"--memory {runner.MEMORY}",
        "no-new-privileges",
        "--userns keep-id",
    ]:
        assert flag in joined
    assert timeout == runner.MAX_TIMEOUT
    assert args[-2:] == ["bash", "/sandbox/main.sh"]


def test_threads_share_the_project_and_workspaces_share_nothing(cfg):
    r = make(cfg)
    go(r.op_write(A, "/project/notes.txt", "kept"))
    go(r.op_write(A, "scratch.txt", "mine"))
    seen = []
    r.podman.effect = lambda m: seen.append(
        {k: sorted(os.listdir(v)) for k, v in m.items() if k in ("/work", "/project")}
    )
    go(r.op_run(A2, "bash", "ls"))
    go(r.op_run(B, "bash", "ls"))
    assert seen[0] == {"/work": [], "/project": ["notes.txt"]}
    assert seen[1] == {"/work": [], "/project": []}


def test_each_workspace_writes_its_own_shared_folder_and_reads_the_others(cfg):
    r = make(cfg)
    go(r.op_write(A, "/shared/career/themes/t/style.css", "body{}"))
    seen = []

    def effect(m):
        seen.append(
            {k: v for k, v in m.items() if k.startswith(("/shared", "/system"))}
        )
        (m["/shared/home"] / "from-home.txt").write_text("h")

    r.podman.effect = effect
    res = go(r.op_run(B, "bash", "ls /shared"))
    assert seen[0] == {
        "/shared/home": shared(cfg, B),
        "/shared/career": shared(cfg, A),
        "/system/themes": cfg.system_themes,
    }
    assert res["changed"] == ["/shared/home/from-home.txt"]
    args = r.podman.runs()[0][0]
    assert f"{shared(cfg, B)}:/shared/home:rw,noexec,nosuid,nodev" in args
    assert f"{shared(cfg, A)}:/shared/career:ro,noexec,nosuid,nodev" in args
    assert f"{cfg.system_themes}:/system/themes:ro,noexec,nosuid,nodev" in args
    assert not any(a.endswith(":/shared") or ":/shared:" in a for a in args)


def test_another_workspaces_shared_folder_is_read_only_to_the_runner_too(cfg):
    r = make(cfg)
    go(r.op_write(A, "/shared/career/notes.txt", "career's"))
    go(r.op_run(B, "bash", "true"))  # home's folders exist
    for op in (
        r.op_write(B, "/shared/career/notes.txt", "x"),
        r.op_write(B, "/shared/career/notes.txt", delete=True),
        r.op_publish(B, "leak", "/shared/career/notes.txt"),
    ):
        with pytest.raises(
            runner.SandboxError, match="career's shared folder, which is read-only"
        ):
            go(op)
    for path in ("/shared", "/shared/", "/system/themes/x"):
        with pytest.raises(runner.SandboxError, match="bad path"):
            go(r.op_write(B, path, "x"))
    assert (shared(cfg, A) / "notes.txt").read_text() == "career's"
    # A symlink in a workspace's own shared folder can't lead its host operations out.
    (shared(cfg, B) / "peek").symlink_to(project(cfg, A))
    with pytest.raises(runner.SandboxError, match="outside /shared/home"):
        go(r.op_write(B, "/shared/home/peek/x", "x"))


def test_runs_in_different_workspaces_overlap(cfg, monkeypatch):
    monkeypatch.setattr(runner, "WAIT", 5)
    events = []

    async def main():
        r = make(cfg, delay=0.2)
        r.podman.effect = lambda m: events.append(("start", m["/project"].parent.name))
        await asyncio.gather(r.op_run(A, "bash", "1"), r.op_run(B, "bash", "2"))
        events.append(("done",))

    go(main())
    assert events[:2] in (
        [("start", "career"), ("start", "home")],
        [("start", "home"), ("start", "career")],
    )


def test_a_workspaces_shared_folder_counts_toward_its_quota(cfg, monkeypatch):
    monkeypatch.setattr(runner, "WORKSPACE_MAX_BYTES", 10)
    r = make(cfg)
    go(r.op_run(A, "bash", "true"))
    (shared(cfg, A) / "big.bin").write_bytes(b"x" * 11)
    with pytest.raises(runner.SandboxError, match=r"biggest: /shared/career/big\.bin"):
        go(r.op_run(A, "python", "1"))
    go(r.op_run(B, "python", "1"))  # other workspaces are unaffected


def test_copy_regular_refuses_symlinks_and_fifos(tmp_path):
    (tmp_path / "real").write_text("r")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(OSError):
        runner.copy_regular(tmp_path / "link", tmp_path / "out1")
    with pytest.raises(runner.SandboxError, match="isn't a regular file"):
        runner.copy_regular(tmp_path / "pipe", tmp_path / "out2")
    runner.copy_regular(tmp_path / "real", tmp_path / "out3")
    assert (tmp_path / "out3").read_text() == "r"


def test_scope_keys_are_checked(cfg):
    r = make(cfg)
    for bad in [
        {"workspace": "../etc", "thread": "1"},
        {"workspace": "a", "thread": ""},
        {"workspace": ".runs", "thread": "1"},
        "career",
    ]:
        with pytest.raises(runner.SandboxError, match="bad|scope"):
            go(r.op_run(bad, "python", "1"))
    with pytest.raises(runner.SandboxError, match="language"):
        go(r.op_run(A, "ruby", "1"))


def test_run_reports_changed_files_by_their_sandbox_paths(cfg):
    def effect(m):
        (m["/work"] / "plot.png").write_bytes(b"png")
        (m["/project"] / "data.csv").write_text("a")
        (m["/project"] / ".local").mkdir(exist_ok=True)
        (m["/project"] / ".local" / "pkg.py").write_text("x")

    res = go(make(cfg, effect=effect).op_run(A, "python", "..."))
    assert res["changed"] == ["/project/data.csv", "/work/plot.png"]
    assert res["warning"] is None


def test_a_run_warns_near_the_limit_counting_hidden_files(cfg, monkeypatch):
    monkeypatch.setattr(runner, "WORKSPACE_WARN_BYTES", 2)

    def effect(m):
        (m["/project"] / ".local").mkdir(exist_ok=True)
        (m["/project"] / ".local" / "pkg.py").write_text("xyz")

    res = go(make(cfg, effect=effect).op_run(A, "python", "..."))
    assert res["changed"] == [] and res["warning"].startswith(
        "this workspace's sandbox uses 0 MB"
    )


def test_timeout_and_oom_are_reported(cfg):
    res = go(
        make(cfg, result=(137, "", "", True)).op_run(
            A, "python", "while True: pass", timeout=5
        )
    )
    assert res["timed_out"] and res["timeout"] == 5 and not res["oom_killed"]
    res = go(
        make(cfg, result=(137, "", "", False), oom=True).op_run(
            A, "python", "x = bytearray(2**40)"
        )
    )
    assert res["oom_killed"]
    res = go(make(cfg, result=(137, "", "", False)).op_run(A, "bash", "kill -9 $$"))
    assert not res["oom_killed"]


def test_capture_keeps_head_and_tail():
    async def main():
        stream = asyncio.StreamReader()
        stream.feed_data(b"start" + b"x" * 300_000 + b"Traceback end")
        stream.feed_eof()
        return await runner.capture(stream, 1000)

    out = go(main())
    assert (
        out.startswith("start") and out.endswith("Traceback end") and "bytes cut" in out
    )
    assert len(out) < 1100


def test_write_takes_sandbox_paths(cfg):
    r = make(cfg)
    assert go(r.op_write(A, "/project/data/in.csv", "a,b\n")) == {
        "path": "/project/data/in.csv",
        "bytes": 4,
    }
    go(r.op_write(A, "/work/a.txt", "a"))
    go(r.op_write(A, "b.txt", "b"))  # relative to /work
    assert (project(cfg, A) / "data/in.csv").read_text() == "a,b\n"
    assert sorted(os.listdir(work(cfg, A))) == ["a.txt", "b.txt"]
    for bad in ["../x", "/etc/passwd", "/workx/a", "a/../../x", "/work", "/project/"]:
        with pytest.raises(runner.SandboxError):
            go(r.op_write(A, bad, "x"))


def test_paths_must_stay_in_their_mount(cfg, tmp_path):
    r = make(cfg)
    go(r.op_write(A, "x.txt", "x"))
    secret = tmp_path / "secret"
    secret.write_text("key")
    (work(cfg, A) / "link").symlink_to(secret)
    (work(cfg, A) / "up").symlink_to(
        project(cfg, A)
    )  # even into /project: each mount stands alone
    for op in (
        r.op_write(A, "link", "x"),
        r.op_write(A, "up/f", "x"),
        r.op_publish(A, "s", "link"),
    ):
        with pytest.raises(runner.SandboxError, match="outside"):
            go(op)
    assert secret.read_text() == "key"


def test_write_refuses_a_fifo_without_blocking(cfg):
    r = make(cfg)
    go(r.op_write(A, "a", "a"))
    os.mkfifo(work(cfg, A) / "pipe")
    (work(cfg, A) / "dir").mkdir()
    for path in ("pipe", "dir"):
        with pytest.raises(runner.SandboxError, match="isn't a regular file"):
            go(asyncio.wait_for(r.op_write(A, path, "x"), 5))


def test_delete(cfg, tmp_path):
    r = make(cfg)
    go(r.op_write(A, "/project/data/sub/a.txt", "a"))
    go(r.op_write(A, "b.txt", "b"))
    assert go(r.op_write(A, "/work/b.txt", delete=True)) == {
        "path": "/work/b.txt",
        "folder": False,
    }
    (project(cfg, A) / "data" / "sub").chmod(0o500)
    assert go(r.op_write(A, "/project/data", delete=True))["folder"]
    assert os.listdir(project(cfg, A)) == []
    # A symlink goes, not what it points to, even outside the workspace.
    secret = tmp_path / "secret"
    secret.write_text("key")
    (work(cfg, A) / "link").symlink_to(secret)
    go(r.op_write(A, "link", delete=True))
    assert secret.read_text() == "key" and not (work(cfg, A) / "link").is_symlink()
    (work(cfg, A) / "dirlink").symlink_to(tmp_path)
    with pytest.raises(runner.SandboxError, match="outside"):
        go(r.op_write(A, "dirlink/secret", delete=True))
    with pytest.raises(runner.SandboxError, match="no '"):
        go(r.op_write(A, "missing", delete=True))
    # Only naming a mount empties it; a blank or relative path to its root is refused.
    for path in ("", " ", ".", "/work/", "/project/."):
        with pytest.raises(runner.SandboxError, match="give the path"):
            go(r.op_write(A, path, delete=True))
    go(r.op_write(A, "/work/ro/f", "x"))
    (work(cfg, A) / "ro").chmod(0o500)
    assert go(r.op_write(A, "/work", delete=True)) == {"path": "/work", "emptied": True}
    assert work(cfg, A).is_dir() and os.listdir(work(cfg, A)) == [] and secret.exists()


def test_quota_stops_runs_and_writes_but_not_deletes(cfg, monkeypatch):
    monkeypatch.setattr(runner, "WORKSPACE_MAX_BYTES", 10)
    r = make(cfg)
    for path, size in [(project(cfg, A) / "big.bin", 9), (work(cfg, A2) / ".cache", 2)]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)  # another chat's scratch counts too
    with pytest.raises(
        runner.SandboxError,
        match=r"over its 0 MB limit.*biggest: /project/big\.bin 0 MB, other chats' /work 0 MB",
    ):
        go(r.op_run(A, "python", "1"))
    with pytest.raises(runner.SandboxError, match="can't write files"):
        go(r.op_write(A, "c", "c"))
    assert r.podman.runs() == []
    go(r.op_run(B, "python", "1"))  # other workspaces are unaffected
    go(r.op_write(A, "/project/big.bin", delete=True))
    assert go(r.op_run(A, "python", "1"))["exit_code"] == 0


def test_gc_removes_idle_threads_but_not_projects(cfg):
    r = make(cfg)
    go(r.op_write(A, "/project/keep", "k"))
    go(r.op_write(A2, "new", "n"))
    past = r.now() - runner.THREAD_MAX_AGE - 10
    os.utime(work(cfg, A), (past, past))
    os.utime(project(cfg, A), (past, past))
    assert r.gc() == ["career/12"]
    assert not work(cfg, A).exists() and work(cfg, A2).exists()
    assert (project(cfg, A) / "keep").exists()


def test_cleanup_removes_leftovers(cfg):
    r = make(cfg)
    (cfg.scripts / "sandbox-abc").mkdir(parents=True)
    go(r.cleanup())
    assert not cfg.scripts.exists()
    assert r.podman.calls[0][0] == ["rm", "-f", "--filter", f"label={runner.LABEL}"]


def test_ping_lists_problems(cfg):
    assert go(make(cfg, result=(1, "", "", False)).op_ping())["problems"]
    r = Runner(
        cfg, podman=lambda args, t, k: asyncio.sleep(0, (0, "true\n", "", False))
    )
    assert go(r.op_ping())["problems"] == []


def test_fast_run_answers_inline(cfg):
    res = go(make(cfg).op_run(A, "python", "print('hi')"))
    assert (
        res["stdout"] == "hi\n"
        and res["run_id"].startswith("r-")
        and "running" not in res
    )


def test_slow_run_goes_to_the_background(cfg, monkeypatch):
    monkeypatch.setattr(runner, "WAIT", 0.05)

    async def main():
        r = make(cfg, delay=0.3)
        first = await r.op_run(A, "python", "slow()")
        # The run holds the workspace: writes and publishes from any of its chats fail at
        # once instead of queueing; another workspace is free.
        for op in [r.op_write(A2, "a", "a"), r.op_publish(A, "p", "a")]:
            with pytest.raises(
                runner.SandboxError, match="still running in this workspace"
            ):
                await asyncio.wait_for(op, 0.05)
        await r.op_write(B, "a", "a")
        monkeypatch.setattr(runner, "WAIT", 5)
        with pytest.raises(runner.SandboxError, match="no such run"):
            await r.op_wait(B, first["run_id"])  # another workspace's run
        done = await r.op_wait(A, first["run_id"])
        again = await r.op_wait(A, first["run_id"])  # still there for an hour
        await r.op_write(A, "a", "a")  # free again
        return first, done, again

    first, done, again = go(main())
    assert first["running"] and "stdout" not in first
    assert (
        done["stdout"] == "hi\n" and done["run_id"] == first["run_id"] and done == again
    )


def test_runs_in_one_workspace_queue(cfg, monkeypatch):
    monkeypatch.setattr(runner, "WAIT", 5)
    order = []

    async def main():
        r = make(cfg, delay=0.1)
        r.podman.effect = lambda m: order.append(m["/work"].name)
        await asyncio.gather(r.op_run(A, "bash", "1"), r.op_run(A2, "bash", "2"))

    go(main())
    assert sorted(order) == ["12", "default"]


def test_unknown_and_expired_runs(cfg):
    r = make(cfg)
    with pytest.raises(runner.SandboxError, match="no such run"):
        go(r.op_wait(A, "r-nope"))
    clock = [1000.0]
    r.now = lambda: clock[0]

    async def main():
        run_id = (await r.op_run(A, "python", "1"))["run_id"]
        await r.op_wait(A, run_id)
        clock[0] += runner.RESULT_KEEP + 1
        await r.op_wait(A, run_id)

    with pytest.raises(runner.SandboxError, match="no such run"):
        go(main())
    assert r._jobs == {}


# --- /public: the workspace's pages, served as they are ---


def test_public_lives_apart_from_the_workspaces_private_folders(cfg):
    seen = {}
    r = make(cfg, effect=lambda m: seen.update(m))
    go(r.op_run(A, "bash", "true"))
    assert seen["/public"] == cfg.public_root / "career" == public(cfg, A)
    assert not seen["/public"].is_relative_to(cfg.root)  # nothing private beside it
    args = r.podman.runs()[0][0]
    assert f"{public(cfg, A)}:/public:rw,noexec,nosuid,nodev" in args


def test_a_page_written_into_public_is_live_at_once_and_runs_say_where(cfg):
    seen_live = []

    def effect(m):
        (m["/public"] / "notes").mkdir()
        (m["/public"] / "notes" / "index.html").write_text(
            "<title>Notes</title><p>hi</p>"
        )
        seen_live.append((public(cfg, A) / "notes" / "index.html").exists())

    r = make(cfg, effect=effect)
    res = go(r.op_run(A, "bash", "make notes"))
    assert seen_live == [True]  # served from where it was written: no copy
    assert res["published"] == {
        "live": [
            {"slug": "notes", "url": "https://ws.example/career/notes/", "blocked": []}
        ]
    }
    assert not (cfg.site_dir / "notes").exists()
    # A run that leaves /public alone says nothing about pages.
    r.podman.effect = None
    assert go(r.op_run(A, "bash", "true"))["published"] is None


def test_writes_say_which_page_changed_or_went(cfg):
    r = make(cfg)
    go(r.op_write(A, "/public/a/index.html", "<title>A</title>"))
    res = go(r.op_write(A, "/public/b.html", "<script>x()</script>"))
    assert res["published"] == {
        "live": [
            {
                "slug": "b.html",
                "url": "https://ws.example/career/b.html",
                "blocked": ["scripts"],
            }
        ]
    }
    res = go(r.op_write(A, "/public/a", delete=True))
    assert res["published"] == {"removed": ["a"]}
    assert go(r.op_write(A, "/project/x", "x")).get("published") is None


def test_a_run_reports_removed_and_changed_pages_but_not_hidden_ones(cfg):
    r = make(cfg)
    go(r.op_write(A, "/public/old/index.html", "o"))
    go(r.op_write(A, "/public/keep.html", "k"))

    def effect(m):
        remove = m["/public"] / "old"
        for f in remove.iterdir():
            f.unlink()
        remove.rmdir()
        (m["/public"] / ".draft").mkdir()
        (m["/public"] / ".draft" / "x.html").write_text("x")
        (m["/public"] / "new.txt").write_text("n")

    r.podman.effect = effect
    res = go(r.op_run(A, "bash", "tidy"))
    assert res["published"] == {
        "live": [
            {
                "slug": "new.txt",
                "url": "https://ws.example/career/new.txt",
                "blocked": [],
            }
        ],
        "removed": ["old"],
    }


def test_public_counts_toward_the_workspaces_quota(cfg, monkeypatch):
    r = make(cfg)
    go(r.op_write(A, "/public/big.html", "x" * 1000))
    monkeypatch.setattr(runner, "WORKSPACE_MAX_BYTES", 500)
    with pytest.raises(runner.SandboxError, match="over its"):
        go(r.op_run(A, "bash", "true"))


def test_each_workspace_has_its_own_public(cfg):
    r = make(cfg)
    go(r.op_write(A, "/public/mine.html", "a"))
    go(
        r.op_write(B, "/public/mine.html", "b")
    )  # no names to claim: each has its prefix
    assert (public(cfg, A) / "mine.html").read_text() == "a"
    assert (public(cfg, B) / "mine.html").read_text() == "b"
    seen = []
    r.podman.effect = lambda m: seen.append(sorted(os.listdir(m["/public"])))
    go(r.op_run(A, "bash", "ls /public"))
    go(r.op_run(B, "bash", "ls /public"))
    assert seen == [["mine.html"], ["mine.html"]]


@pytest.mark.parametrize(
    ("html", "description"),
    [
        (
            '<meta name="description" content="Ferries &amp; hikes"><p>no</p>',
            "Ferries & hikes",
        ),
        (
            "<style>p{}</style><p> </p><p>First <b>real</b>\n line</p>",
            "First real line",
        ),
        ("<h1>Only a heading</h1>", ""),
    ],
)
def test_page_description(html, description):
    assert runner.page_description(html) == description


def test_publish_gives_a_pages_address_and_card(cfg):
    r = make(cfg)
    go(
        r.op_write(
            A,
            "/public/plot/index.html",
            "<title>Plot &amp; notes</title><img src=plot.svg>",
        )
    )
    go(r.op_write(A, "/public/plot/plot.svg", "<svg/>"))
    for args in (("plot",), ("", "/public/plot/plot.svg")):
        res = go(r.op_publish(A, *args))
        card = res.pop("card")
        assert re.fullmatch(
            r"\[!\[Plot & notes\]\(https://pages\.example/_cards/\w+\.png\?v=\w+\)\]\(https://ws\.example/career/plot/\)",
            card,
        )
        assert res == {
            "slug": "plot",
            "url": "https://ws.example/career/plot/",
            "files": 2,
            "blocked": [],
        }
    assert (cfg.site_dir / "_cards").is_dir()  # cards stay on the pages site
    assert not (cfg.site_dir / "plot").exists()


def test_publish_copies_into_public_and_remove_deletes_from_it(cfg):
    r = make(cfg)
    go(r.op_write(A, "trip/index.html", "<title>Trip</title>"))
    go(r.op_write(A, "trip/.hidden/x", "x"))
    res = go(r.op_publish(A, "trip-plan", "/work/trip"))
    assert res["url"] == "https://ws.example/career/trip-plan/"
    assert os.listdir(public(cfg, A) / "trip-plan") == ["index.html"]
    assert not [p for p in os.listdir(public(cfg, A)) if p.startswith(".")]
    go(r.op_write(A, "/project/report.html", "<p>hi</p>"))
    go(r.op_publish(A, "report", "/project/report.html"))
    assert (public(cfg, A) / "report" / "index.html").read_text() == "<p>hi</p>"
    assert go(r.op_publish(A2, "trip-plan", remove=True)) == {
        "slug": "trip-plan",
        "removed": True,
    }  # any chat of the workspace
    assert not (public(cfg, A) / "trip-plan").exists()
    with pytest.raises(runner.SandboxError, match="no page"):
        go(r.op_publish(A, "trip-plan", remove=True))


def test_publish_without_a_page_lists_them(cfg):
    r = make(cfg)
    go(r.op_write(A, "/public/a/index.html", "a"))
    go(r.op_write(A, "/public/data.csv", "1"))
    go(r.op_write(A, "/public/.draft", "x"))
    assert go(r.op_publish(A)) == {
        "site": "https://ws.example/career/",
        "pages": [
            {"slug": "a", "url": "https://ws.example/career/a/"},
            {"slug": "data.csv", "url": "https://ws.example/career/data.csv"},
        ],
    }


def test_links_on_the_sites_own_origin_arent_blocked(cfg):
    r = make(cfg)
    res = go(
        r.op_write(
            A,
            "/public/s/index.html",
            '<link rel="stylesheet" href="https://ws.example/career/lab/a.css">'
            '<img src="https://ws.example.evil/x.png">',
        )
    )
    assert res["published"]["live"][0]["blocked"] == [
        "images, media or frames from another host"
    ]


def test_publish_refuses_bad_input(cfg, tmp_path, monkeypatch):
    r = make(cfg)
    go(r.op_write(A, "a.txt", "abc"))
    for slug in ["", "Has-Caps", "-x", "a/b", "../x"]:
        with pytest.raises(runner.SandboxError, match="slug must"):
            go(r.op_publish(A, slug, "a.txt"))
    for slug, path, msg in [
        ("s", "missing", "no 'missing'"),
        ("s", "/work", "all of /work"),
        ("s", "", "nothing at /public/s"),
        ("", "/public", "not all of it"),
    ]:
        with pytest.raises(runner.SandboxError, match=msg):
            go(r.op_publish(A, slug, path))
    (work(cfg, A) / "empty").mkdir()
    with pytest.raises(runner.SandboxError, match="no files"):
        go(r.op_publish(A, "s", "empty"))
    # Symlinks inside a copied folder are left out.
    victim = tmp_path / "victim"
    victim.mkdir()
    go(r.op_write(A, "dir/f.txt", "f"))
    (work(cfg, A) / "dir" / "leak").symlink_to(victim)
    go(r.op_publish(A, "d", "dir"))
    assert sorted(os.listdir(public(cfg, A) / "d")) == ["f.txt"]
    monkeypatch.setattr(runner, "PUBLISH_MAX_BYTES", 2)
    with pytest.raises(runner.SandboxError, match="the most publish copies"):
        go(r.op_publish(A, "s2", "a.txt"))


def test_config_follows_this_machines_host_settings(monkeypatch):
    for var in (
        "SANDBOX_SOCKET",
        "SANDBOX_BUILD_SOCKET",
        "SANDBOX_ROOT",
        "SANDBOX_SYSTEM_THEMES",
        "SANDBOX_SITE_DIR",
        "SANDBOX_SITE_URL",
        "SANDBOX_PUBLIC",
        "SANDBOX_PUBLIC_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    config = runner.Config.from_env()
    assert config.site_url == "http://127.0.0.1:8445/"
    assert config.public_url == "http://127.0.0.1:8447/"
    monkeypatch.setenv("ANYTHINGLLM_STORAGE", "/data/allm")
    monkeypatch.setenv("PUBLIC_HOST", "box.tail.ts.net")
    config = runner.Config.from_env()
    assert config.socket == Path("/data/allm/everythingllm/sandbox/runner.sock")
    assert config.build_socket == Path(
        "/data/allm/everythingllm/sandbox-build/runner.sock"
    )
    data = Path("~/.local/share/everythingllm").expanduser()
    assert config.site_dir == data / "pages" / "public"
    assert config.site_url == "https://box.tail.ts.net:8445/"
    assert config.root == data / "sandbox" / "workspaces"
    assert config.public_root == data / "sandbox" / "public"
    assert config.public_url == "https://box.tail.ts.net:8447/"
    assert config.system_themes == runner.SYSTEM_ZOLA / "themes"
    assert (config.system_themes / "agent-site" / "theme.toml").is_file()


def test_the_workspace_pages_site_serves_public_as_it_is():
    repo = Path(__file__).resolve().parents[3]
    caddy = (repo / "host" / "caddy" / "pages.Caddyfile").read_text()
    unit = (repo / "host" / "quadlet" / "static_agent.container.in").read_text()
    assert (
        "Volume=%h/.local/share/everythingllm/sandbox/public:/srv/workspaces:ro" in unit
    )
    assert "PublishPort=127.0.0.1:8447:8447" in unit
    workspaces = caddy[caddy.index(":8447 {") :]
    assert "root * /srv/workspaces" in workspaces
    assert (
        "script-src 'none'" in workspaces
    )  # scripts stay off until the switch is used
    assert "@scripts expression false" in workspaces


# --- site builds ---


def built(m):
    """What a successful build leaves in /out: a page, an asset and a planted symlink."""
    out = m["/out"] / "site"
    (out / "notes").mkdir(parents=True)
    (out / "index.html").write_text("<title>Site</title><p>home</p>")
    (out / "notes" / "index.html").write_text("<p>n</p>")
    (out / "leak").symlink_to("/etc/passwd")


def test_a_site_builds_without_network_into_public(cfg):
    r = make(cfg, effect=built)
    go(r.op_write(A, "/project/sites/portfolio/zola.toml", 'theme = "agent-site"'))
    res = go(r.op_build_site(A, "/project/sites/portfolio"))
    assert res["slug"] == "portfolio" and res["files"] == 2
    assert res["url"] == "https://ws.example/career/portfolio/"
    assert [p["slug"] for p in res["published"]["live"]] == ["portfolio"]
    assert sorted(os.listdir(public(cfg, A) / "portfolio")) == ["index.html", "notes"]
    args = r.podman.runs()[0][0]
    assert args[args.index("--network") + 1] == "none" and "--dns" not in args
    assert args[-4:] == [
        "python",
        "/sandbox/sitebuild.py",
        "/project/sites/portfolio",
        "https://ws.example/career/portfolio",
    ]
    m = mounts(args)
    assert set(m) == {
        "/work",
        "/project",
        "/shared/career",
        "/system/themes",
        "/out",
        "/sandbox",
    }
    for a in args:
        if a.endswith(
            (":/work:ro,noexec,nosuid,nodev", ":/project:ro,noexec,nosuid,nodev")
        ):
            break
    else:
        raise AssertionError("the workspace's folders aren't mounted read-only")
    assert not any(":/public" in a for a in args)
    assert not any(cfg.scripts.iterdir())  # the run folder is gone


def test_a_build_from_shared_takes_a_slug_and_another_workspaces_themes(cfg):
    r = make(cfg, effect=built)
    go(r.op_write(B, "/shared/home/themes/t/theme.toml", ""))
    go(r.op_write(A, "/shared/career/sites/lab/zola.toml", 'theme = "t"'))
    res = go(r.op_build_site(A, "/shared/career/sites/lab/", slug="my-lab"))
    assert res["url"] == "https://ws.example/career/my-lab/"
    assert (
        f"{shared(cfg, B)}:/shared/home:ro,noexec,nosuid,nodev" in r.podman.runs()[0][0]
    )


def test_a_failed_build_publishes_nothing_and_says_why(cfg):
    r = make(cfg, result=(1, "", "Error: Failed to render 'index.html'\n", False))
    go(r.op_write(A, "/project/s/zola.toml", ""))
    with pytest.raises(
        runner.SandboxError, match="didn't build: Error: Failed to render"
    ):
        go(r.op_build_site(A, "/project/s"))
    assert not (public(cfg, A) / "s").exists() and not (cfg.site_dir / "s").exists()
    r = make(cfg, result=(137, "", "", True))
    with pytest.raises(runner.SandboxError, match="took over"):
        go(r.op_build_site(A, "/project/s"))
    r = make(cfg)  # exits 0 but writes nothing
    with pytest.raises(runner.SandboxError, match="produced no files"):
        go(r.op_build_site(A, "/project/s"))


def test_what_a_build_refuses(cfg):
    r = make(cfg)
    go(r.op_write(A, "/project/notasite/readme.md", "x"))
    go(r.op_write(B, "/shared/home/s/zola.toml", ""))
    go(r.op_write(A, "/public/p/zola.toml", ""))
    for path, slug, why in [
        ("/project/notasite", "", "no zola.toml"),
        ("/shared/home/s", "", "home's shared folder, which is read-only"),
        ("/public/p", "", "not from /public"),
        ("/project/Bad_Name", "", "isn't a page name"),
        ("/project/notasite", "x/y", "isn't a page name"),
    ]:
        with pytest.raises(runner.SandboxError, match=why):
            go(r.op_build_site(A, path, slug))
    assert r.podman.runs() == []


# --- system sites ---


def system_cfg(cfg, tmp_path, theme_from='theme_from = "system"'):
    site = tmp_path / "sites" / "status"
    site.mkdir(parents=True)
    (site / "zola.toml").write_text(
        f'theme = "agent-site"\n[extra.build]\n{theme_from}\n'
    )
    (tmp_path / "sites" / "news").mkdir()
    (tmp_path / "sites" / "news" / "zola.toml").write_text('theme = "agent-site"\n')
    (tmp_path / "entries" / "status" / "reports").mkdir(parents=True)
    cfg.sites_source = tmp_path / "sites"
    cfg.sites_content = tmp_path / "entries"
    return cfg


def test_a_system_site_builds_in_the_sandbox_into_its_staging_folder(cfg, tmp_path):
    cfg = system_cfg(cfg, tmp_path)
    r = make(cfg)
    # A run first, so career's folders exist and its shared folder is mounted.
    go(r.op_run(A, "bash", "true"))
    r.podman.effect = built
    res = go(r.op_build_system_site("status"))
    new = cfg.site_dir / ".status.new"
    assert res == {"site": "status", "path": str(new), "files": 2}
    assert sorted(str(p.relative_to(new)) for p in new.rglob("*") if p.is_file()) == [
        "index.html",
        "notes/index.html",
    ]  # the planted symlink is left out
    args = r.podman.runs()[-1][0]
    assert args[args.index("--network") + 1] == "none"
    m = mounts(args)
    assert m["/site"] == tmp_path / "sites" / "status"
    assert m["/entries"] == tmp_path / "entries" / "status"
    assert m["/shared/career"] == shared(cfg, A) and "/work" not in m
    assert f"{tmp_path / 'entries' / 'status'}:/entries:ro,noexec,nosuid,nodev" in args
    assert args[-5:] == [
        "python",
        "/sandbox/sitebuild.py",
        "/site",
        "https://pages.example/status",
        "/entries",
    ]


def test_a_system_site_copy_never_follows_a_symlink_planted_on_the_way(
    cfg, tmp_path, monkeypatch
):
    """The pages site is writable by the sites and research containers as the same user,
    so one could put a symlink in .status.new while the host copies into it."""
    cfg = system_cfg(cfg, tmp_path)
    r = make(cfg)
    r.podman.effect = built
    outside = tmp_path / "outside"
    outside.mkdir()
    new = cfg.site_dir / ".status.new"
    copy = runner.copy_into

    def racing(source, folder, name):
        copy(source, folder, name)
        if not (new / "notes").exists():  # after the first file, before notes/
            (new / "notes").symlink_to(outside)

    monkeypatch.setattr(runner, "copy_into", racing)
    with pytest.raises(runner.SandboxError, match="couldn't copy the built site"):
        go(r.op_build_system_site("status"))
    assert list(outside.iterdir()) == []

    # Nor over a file planted where one is about to go (a hardlink, say).
    monkeypatch.setattr(runner, "copy_into", copy)
    planted = outside / "target"
    planted.write_text("host file")

    def plant(name, *args, **kwargs):
        made = real_mkdir(name, *args, **kwargs)
        if name == "notes":
            os.link(planted, new / "notes" / "index.html")
        return made

    real_mkdir = os.mkdir
    monkeypatch.setattr(runner.os, "mkdir", plant)
    with pytest.raises(runner.SandboxError, match="couldn't copy the built site"):
        go(r.op_build_system_site("status"))
    assert planted.read_text() == "host file"


def test_the_build_socket_serves_only_system_site_builds(cfg, tmp_path, monkeypatch):
    """The sites and research containers mount the build socket, not the runner's: the
    runner takes the scope a caller names, so the full socket would let a container act as
    any workspace."""
    cfg = system_cfg(cfg, tmp_path)
    # AF_UNIX paths are short, so not tmp_path.
    base = Path("/tmp") / f"sandbox-test-{os.getpid()}"
    cfg.socket, cfg.build_socket = base / "runner.sock", base / "build.sock"
    made = []

    def fake_runner(config):
        made.append(make(config, effect=built))
        return made[-1]

    monkeypatch.setattr(runner, "Runner", fake_runner)

    async def check():
        stop = asyncio.Event()
        task = asyncio.create_task(runner.serve(cfg, stop))
        while not (cfg.socket.exists() and cfg.build_socket.exists()):
            await asyncio.sleep(0.01)
        try:
            ask = hostrpc.request
            assert await ask(cfg.build_socket, "ping", {}, 5) == {}
            for op, args in [
                ("run", {"scope": A, "language": "bash", "code": "true"}),
                ("write", {"scope": A, "path": "/project/x", "content": "x"}),
                ("publish", {"scope": A, "slug": "x", "path": "x"}),
                ("build_site", {"scope": A, "path": "/project/s"}),
            ]:
                with pytest.raises(hostrpc.RunnerError, match=f"unknown op '{op}'"):
                    await ask(cfg.build_socket, op, args, 5)
            res = await ask(
                cfg.build_socket, "build_system_site", {"site": "status"}, 5
            )
            assert res["path"] == str(cfg.site_dir / ".status.new")
            # The runner's own socket still has every op.
            await ask(cfg.socket, "write", {"scope": A, "path": "x", "content": "x"}, 5)
        finally:
            stop.set()
            await task
        assert not cfg.socket.exists() and not cfg.build_socket.exists()

    try:
        go(check())
    finally:
        shutil.rmtree(base, ignore_errors=True)
    assert len(made) == 1  # one runner behind both


def test_what_a_system_site_build_refuses(cfg, tmp_path):
    cfg = system_cfg(cfg, tmp_path)
    r = make(cfg)
    for site, why in [
        ("news", "names no \\[extra.build\\] theme_from; it builds on the host"),
        ("nope", "no system site 'nope'"),
        ("../status", "bad site"),
        ("Status", "bad site"),
    ]:
        with pytest.raises(runner.SandboxError, match=why):
            go(r.op_build_system_site(site))
    assert r.podman.runs() == []
    r = make(cfg, result=(1, "", "Error: Failed to render\n", False))
    with pytest.raises(
        runner.SandboxError,
        match="zola build failed for status:\nError: Failed to render",
    ):
        go(r.op_build_system_site("status"))
    assert not (cfg.site_dir / ".status.new").exists()


def test_a_system_site_cannot_follow_a_workspace_theme(cfg, tmp_path):
    # Until a workspace's theme can be pinned (docs/.proposals/shared-sites.md, Decision 2), a system
    # site never builds from a workspace's live /shared.
    cfg = system_cfg(cfg, tmp_path, theme_from='theme_from = "career"')
    r = make(cfg)
    with pytest.raises(runner.SandboxError, match="can only use 'system' until"):
        go(r.op_build_system_site("status"))
    assert r.podman.runs() == []
