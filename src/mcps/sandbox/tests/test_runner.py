import asyncio
import itertools
import json
import os
import re
from pathlib import Path

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
    return Config(
        socket=tmp_path / "sock" / "runner.sock",
        root=tmp_path / "sandbox",
        shared=tmp_path / "shared",
        site_dir=tmp_path / "site",
        site_url="https://pages.example/",
    )


def make(cfg, **kw):
    return Runner(cfg, podman=FakePodman(**kw))


def go(coro):
    return asyncio.run(coro)


def work(cfg, scope):
    return cfg.root / scope["workspace"] / "threads" / scope["thread"]


def project(cfg, scope):
    return cfg.root / scope["workspace"] / "project"


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
    assert any(a.endswith(":/sandbox:ro") for a in args) and any(
        a.endswith(":/pages:ro") for a in args
    )
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


def test_every_workspace_mounts_the_same_shared_folder_as_data(cfg):
    r = make(cfg)
    go(r.op_write(A, "/shared/notes.txt", "for everyone"))
    seen = []

    def effect(m):
        seen.append(sorted(os.listdir(m["/shared"])))
        (m["/shared"] / "from-b.txt").write_text("b")

    r.podman.effect = effect
    res = go(r.op_run(B, "bash", "ls /shared"))
    assert seen == [["notes.txt"]] and res["changed"] == ["/shared/from-b.txt"]
    assert (cfg.shared / "from-b.txt").read_text() == "b"
    args = r.podman.runs()[0][0]
    assert f"{cfg.shared}:/shared:rw,noexec,nosuid,nodev" in args
    # It isn't inside any workspace's folder.
    assert not (cfg.root / "career" / "shared").exists()
    assert go(r.op_write(A, "/shared/notes.txt", delete=True)) == {
        "path": "/shared/notes.txt",
        "folder": False,
    }


def test_shared_paths_must_stay_in_shared(cfg):
    r = make(cfg)
    go(r.op_write(A, "/project/secret", "career's"))
    cfg.shared.mkdir(exist_ok=True)
    (cfg.shared / "peek").symlink_to(
        project(cfg, A)
    )  # planted by a run in another workspace
    for op in (
        r.op_write(B, "/shared/peek/secret", "x"),
        r.op_publish(B, "leak", "/shared/peek"),
    ):
        with pytest.raises(runner.SandboxError, match="outside /shared"):
            go(op)
    assert (project(cfg, A) / "secret").read_text() == "career's"


def test_runs_take_turns_across_workspaces_and_shared_writes_wait_for_none(
    cfg, monkeypatch
):
    monkeypatch.setattr(runner, "WAIT", 0.05)
    order = []

    async def main():
        r = make(cfg, delay=0.2)
        r.podman.effect = lambda m: order.append(m["/project"].parent.name)
        first = await r.op_run(A, "python", "slow()")
        # Anything under /shared fails at once while any workspace runs code...
        for op in [
            r.op_write(B, "/shared/x", "x"),
            r.op_write(B, "/shared/x", delete=True),
            r.op_publish(B, "p", "/shared/x"),
        ]:
            with pytest.raises(runner.SandboxError, match="can change /shared"):
                await asyncio.wait_for(op, 0.05)
        # ...the rest of another workspace doesn't wait,
        await r.op_write(B, "a", "a")
        # and its run queues behind the first.
        monkeypatch.setattr(runner, "WAIT", 5)
        await asyncio.gather(r.op_wait(A, first["run_id"]), r.op_run(B, "bash", "2"))
        await r.op_write(B, "/shared/x", "x")

    go(main())
    assert order == ["career", "home"]


def test_shared_quota_stops_runs_and_writes_there(cfg, monkeypatch):
    monkeypatch.setattr(runner, "SHARED_MAX_BYTES", 10)
    r = make(cfg)
    cfg.shared.mkdir(exist_ok=True)
    (cfg.shared / "big.bin").write_bytes(b"x" * 11)
    with pytest.raises(
        runner.SandboxError, match=r"/shared uses 0 MB.*biggest: /shared/big\.bin 0 MB"
    ):
        go(r.op_run(B, "python", "1"))
    with pytest.raises(runner.SandboxError, match="can write there"):
        go(r.op_write(A, "/shared/c", "c"))
    go(r.op_write(A, "/project/c", "c"))  # a workspace's own folders are unaffected
    go(r.op_write(B, "/shared/big.bin", delete=True))
    assert go(r.op_run(B, "python", "1"))["exit_code"] == 0


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


# --- publishing ---


def test_publish_a_folder_as_a_page(cfg):
    r = make(cfg)
    go(
        r.op_write(
            A, "out/index.html", "<title>Plot &amp; notes</title><img src=plot.svg>"
        )
    )
    go(r.op_write(A, "out/plot.svg", "<svg/>"))
    go(r.op_write(A, "out/.hidden/x", "x"))
    res = go(r.op_publish(A, "plot", "/work/out"))
    card = res.pop("card")
    assert re.fullmatch(
        r"\[!\[Plot & notes\]\(https://pages\.example/_cards/(\w+\.png)\?v=\w+\)\]\(https://pages\.example/plot/\)",
        card,
    )
    assert (cfg.site_dir / "_cards").is_dir()
    assert res == {
        "slug": "plot",
        "url": "https://pages.example/plot/",
        "files": 3,
        "blocked": [],
    }
    page = cfg.site_dir / "plot"
    assert (page / "plot.svg").read_text() == "<svg/>"
    assert json.loads((page / ".page").read_text()) == {
        "workspace": "career",
        "title": "Plot & notes",
        "entry": "index.html",
    }
    assert (page / "plot.svg").stat().st_mode & 0o777 == 0o644
    index = (cfg.site_dir / "index.html").read_text()
    assert '<a href="plot/">Plot &amp; notes</a>' in index and "career" in index
    assert (cfg.site_dir / ".page").exists()
    assert not [
        p for p in cfg.site_dir.iterdir() if p.name.startswith(".plot")
    ]  # no temp folders left


def test_publish_a_file(cfg):
    r = make(cfg)
    go(r.op_write(A, "/project/report.html", "<p>hi</p><script>x()</script>"))
    go(r.op_write(A, "my data.csv", "a"))
    res = go(r.op_publish(A, "report", "/project/report.html"))
    assert res["url"] == "https://pages.example/report/" and res["blocked"] == [
        "scripts"
    ]
    assert (cfg.site_dir / "report" / "index.html").exists()
    res = go(r.op_publish(A, "data", "my data.csv"))
    assert res["url"] == "https://pages.example/data/my%20data.csv"
    assert (
        '<a href="data/my%20data.csv">data</a>'
        in (cfg.site_dir / "index.html").read_text()
    )


def test_republishing_replaces_and_remove_unpublishes(cfg):
    r = make(cfg)
    go(r.op_write(A, "a/index.html", "one"))
    go(r.op_write(A, "a/old.png", "x"))
    go(r.op_publish(A, "p", "a"))
    go(r.op_write(A, "a/old.png", delete=True))
    go(r.op_write(A, "a/index.html", "two"))
    go(r.op_publish(A, "p", "a"))
    assert sorted(os.listdir(cfg.site_dir / "p")) == [".page", "index.html"]
    assert len(os.listdir(cfg.site_dir / "_cards")) == 1  # the card was replaced too
    assert go(r.op_publish(A2, "p", remove=True)) == {
        "slug": "p",
        "removed": True,
    }  # any chat of the workspace
    assert not (cfg.site_dir / "p").exists()
    assert not os.listdir(cfg.site_dir / "_cards")
    assert "Nothing published yet" in (cfg.site_dir / "index.html").read_text()
    with pytest.raises(runner.SandboxError, match="no page"):
        go(r.op_publish(A, "p", remove=True))


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


def test_a_page_belongs_to_its_workspace(cfg):
    r = make(cfg)
    go(r.op_write(A, "x.html", "x"))
    go(r.op_write(B, "y.html", "y"))
    go(r.op_publish(A, "mine", "x.html"))
    for op in (r.op_publish(B, "mine", "y.html"), r.op_publish(B, "mine", remove=True)):
        with pytest.raises(runner.SandboxError, match="taken"):
            go(op)
    # Anything on the site that isn't a page is taken too: a Zola site, the podcasts.
    (cfg.site_dir / "news").mkdir()
    (cfg.site_dir / "news" / ".zola-site").touch()
    (cfg.site_dir / "unowned").mkdir()
    (cfg.site_dir / "unowned" / ".page").write_text(
        "career\n"
    )  # not a marker this runner wrote
    for slug in ("news", "unowned", "index.html"):
        with pytest.raises(runner.SandboxError, match="taken|slug must"):
            go(r.op_publish(A, slug, "x.html"))
    # Each workspace's runs see its own pages, read-only.
    seen = []
    r.podman.effect = lambda m: seen.append({k for k in m if k.startswith("/pages/")})
    go(r.op_run(A, "bash", "ls /pages"))
    go(r.op_run(B, "bash", "ls /pages"))
    assert seen == [{"/pages/mine"}, set()]
    args = r.podman.runs()[0][0]
    assert f"{cfg.site_dir / 'mine'}:/pages/mine:ro" in args


def test_a_page_published_from_shared_belongs_to_every_workspace(cfg):
    r = make(cfg)
    go(r.op_write(A, "/shared/lab/index.html", "<title>Lab</title>one"))
    go(r.op_publish(A, "lab", "/shared/lab"))
    assert json.loads((cfg.site_dir / "lab" / ".page").read_text())["workspace"] == (
        runner.SHARED_OWNER
    )
    assert "<span>shared · " in (cfg.site_dir / "index.html").read_text()
    # Another workspace replaces it from /shared...
    go(r.op_write(B, "/shared/lab/index.html", "<title>Lab</title>two"))
    go(r.op_publish(B, "lab", "/shared/lab"))
    assert (cfg.site_dir / "lab" / "index.html").read_text().endswith("two")
    # ...but not from its own folders, which would make it that workspace's.
    go(r.op_write(B, "/project/mine.html", "mine"))
    with pytest.raises(runner.SandboxError, match="replaced only from /shared"):
        go(r.op_publish(B, "lab", "/project/mine.html"))
    # Nor can /shared take a workspace's page.
    go(r.op_publish(B, "minepage", "/project/mine.html"))
    with pytest.raises(runner.SandboxError, match="taken"):
        go(r.op_publish(A, "minepage", "/shared/lab"))
    # Every workspace's runs see the shared page.
    seen = []
    r.podman.effect = lambda m: seen.append({k for k in m if k.startswith("/pages/")})
    go(r.op_run(A, "bash", "ls /pages"))
    go(r.op_run(B, "bash", "ls /pages"))
    assert seen == [{"/pages/lab"}, {"/pages/lab", "/pages/minepage"}]
    # Any workspace can take it down.
    assert go(r.op_publish(A, "lab", remove=True))["removed"]


def test_links_on_the_sites_own_origin_arent_blocked(cfg):
    r = make(cfg)
    go(
        r.op_write(
            A,
            "site/index.html",
            '<link rel="stylesheet" href="https://pages.example/lab/a.css">'
            '<img src="https://pages.example.evil/x.png">',
        )
    )
    res = go(r.op_publish(A, "s", "site"))
    assert res["blocked"] == ["images, media or frames from another host"]


def test_publish_refuses_bad_input(cfg, tmp_path, monkeypatch):
    r = make(cfg)
    go(r.op_write(A, "a.txt", "abc"))
    for slug in ["", "Has-Caps", "-x", "a/b", "../x"]:
        with pytest.raises(runner.SandboxError, match="slug must"):
            go(r.op_publish(A, slug, "a.txt"))
    for path, msg in [
        ("missing", "no 'missing'"),
        ("/work", "all of /work"),
        ("", "give the path"),
    ]:
        with pytest.raises(runner.SandboxError, match=msg):
            go(r.op_publish(A, "s", path))
    (work(cfg, A) / "empty").mkdir()
    with pytest.raises(runner.SandboxError, match="no files"):
        go(r.op_publish(A, "s", "empty"))
    # A symlink planted in the site folder isn't followed: the slug is taken.
    victim = tmp_path / "victim"
    victim.mkdir()
    (cfg.site_dir / "s").symlink_to(victim)
    with pytest.raises(runner.SandboxError, match="taken"):
        go(r.op_publish(A, "s", "a.txt"))
    assert not any(victim.iterdir())
    # Symlinks inside a published folder are left out.
    go(r.op_write(A, "dir/f.txt", "f"))
    (work(cfg, A) / "dir" / "leak").symlink_to(tmp_path / "victim")
    go(r.op_publish(A, "d", "dir"))
    assert sorted(os.listdir(cfg.site_dir / "d")) == [".page", "f.txt"]
    monkeypatch.setattr(runner, "PUBLISH_MAX_BYTES", 2)
    with pytest.raises(runner.SandboxError, match="the most publish copies"):
        go(r.op_publish(A, "s2", "a.txt"))


def test_config_follows_this_machines_host_settings(monkeypatch):
    for var in (
        "SANDBOX_SOCKET",
        "SANDBOX_ROOT",
        "SANDBOX_SHARED",
        "SANDBOX_SITE_DIR",
        "SANDBOX_SITE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    assert runner.Config.from_env().site_url == "http://127.0.0.1:8445/"
    monkeypatch.setenv("ANYTHINGLLM_STORAGE", "/data/allm")
    monkeypatch.setenv("PUBLIC_HOST", "box.tail.ts.net")
    config = runner.Config.from_env()
    assert config.socket == Path("/data/allm/sandbox/runner.sock")
    assert config.site_dir == Path("/data/allm/site")
    assert config.site_url == "https://box.tail.ts.net:8445/"
    assert config.root == Path("~/.local/share/everythingllm/sandbox").expanduser()
    assert config.shared == Path("~/.local/share/everythingllm/shared").expanduser()


def test_the_pages_site_lets_marked_pages_use_inline_css():
    caddy = (
        Path(__file__).resolve().parents[4] / "host" / "caddy" / "pages.Caddyfile"
    ).read_text()
    assert f"@page file /{{path.0}}/{runner.PAGE_MARKER}\n" in caddy
    assert f"hide {runner.PAGE_MARKER} .zola-site" in caddy
