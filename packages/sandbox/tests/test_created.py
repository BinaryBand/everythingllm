"""Created files (sandbox.created), as a client with the user's developer API key reads and
changes them through the apps server (sandbox.appsweb)."""

import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest
from sandbox import appsweb, created, workspace
from test_runner import A, cfg, go, make, project, work  # noqa: F401 - cfg is a fixture


@pytest.fixture
def r(cfg):  # noqa: F811
    return make(cfg)


async def check(key: str):
    return None if key == "KEY" else (403, "no")


async def served(r):
    server = await appsweb.AppsWeb(r, check).serve(0)
    return server, server.sockets[0].getsockname()[1]


async def call(r, method: str, path: str, key: str | None = "KEY"):
    server, port = await served(r)
    head = f"{method} {path} HTTP/1.1\r\nHost: x\r\n"
    if key is not None:
        head += f"Authorization: Bearer {key}\r\n"
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head.encode() + b"\r\n")
    await writer.drain()
    data = await reader.read()
    writer.close()
    server.close()
    top, _, payload = data.partition(b"\r\n\r\n")
    return int(top.split()[1]), payload, top.decode()


def put(path, data=b"x", age=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if age:
        old = time.time() - age
        os.utime(path, (old, old))


def files(r, cfg):  # noqa: F811
    status, body, _ = go(call(r, "GET", "/_live/sandbox/files/career"))
    assert status == 200
    return json.loads(body)


def make_files(cfg):  # noqa: F811
    put(project(cfg, A) / "report.pdf", b"pdf", age=100)
    put(project(cfg, A) / "downloads" / "a.csv", b"1,2", age=50)
    put(work(cfg, A) / "scratch" / "out.txt", b"hello", age=10)
    put(project(cfg, A) / "apps" / "list" / "data.json", b"{}")
    put(project(cfg, A) / ".cache" / "junk", b"x")
    put(cfg.root / "career" / "shared" / "notes.md", b"n")
    put(cfg.public_root / "career" / "site" / "index.html", b"<p>")
    put(cfg.public_root / "career" / "apps" / "list" / "index.html", b"<p>")


def test_lists_every_kind_newest_first_without_apps_or_hidden(r, cfg):  # noqa: F811
    make_files(cfg)
    got = files(r, cfg)
    ids = [f["id"] for f in got["files"]]
    assert sorted(ids) == sorted(
        [
            "project/report.pdf",
            "project/downloads/a.csv",
            "threads/12/scratch/out.txt",
            "shared/notes.md",
            "public/site/index.html",
        ]
    )
    by = {f["id"]: f for f in got["files"]}
    thread = by["threads/12/scratch/out.txt"]
    assert thread["area"] == "work" and thread["thread"] == "12"
    assert thread["name"] == "out.txt" and thread["dir"] == "scratch"
    assert thread["expires"] > time.time()
    assert by["public/site/index.html"]["url"].endswith("/career/site/index.html")
    assert "expires" not in by["project/report.pdf"]
    assert got["limit"] == workspace.WORKSPACE_MAX_BYTES and got["used"] > 0
    assert got["truncated"] is False
    mtimes = [f["modified"] for f in got["files"]]
    assert mtimes == sorted(mtimes, reverse=True)


def test_a_key_is_needed_and_a_workspace_without_a_sandbox_is_404(r, cfg):  # noqa: F811
    make_files(cfg)
    assert go(call(r, "GET", "/_live/sandbox/files/career", key=None))[0] == 401
    assert go(call(r, "GET", "/_live/sandbox/files/career", key="BAD"))[0] == 403
    assert go(call(r, "GET", "/_live/sandbox/files/nobody"))[0] == 404
    assert go(call(r, "POST", "/_live/sandbox/files/career"))[0] == 405
    assert go(call(r, "GET", "/_live/sandbox/files/career/keep?path=x"))[0] == 405


def test_download_sends_the_file_as_an_attachment(r, cfg):  # noqa: F811
    make_files(cfg)
    status, body, top = go(
        call(r, "GET", "/_live/sandbox/files/career/download?path=project/report.pdf")
    )
    assert status == 200 and body == b"pdf"
    assert "filename*=UTF-8''report.pdf" in top
    assert "Access-Control-Allow-Origin: *" in top


@pytest.mark.parametrize(
    "bad",
    [
        "project/apps/list/data.json",
        "project/.cache/junk",
        "project/../threads/12/scratch/out.txt",
        "project/nope.txt",
        "project",
        "threads/12",
        "threads/../x/y",
        "etc/passwd",
        "project/report.pdf%00x",
        "threads/12/scratch%00/out.txt",
        "",
    ],
)
def test_nothing_else_can_be_named(r, cfg, bad):  # noqa: F811
    make_files(cfg)
    path = f"/_live/sandbox/files/career/download?path={bad}"
    assert go(call(r, "GET", path))[0] == 404
    assert go(call(r, "POST", path.replace("download", "delete")))[0] == 404


def test_a_symlink_is_never_followed(r, cfg, tmp_path):  # noqa: F811
    make_files(cfg)
    secret = tmp_path / "secret.txt"
    secret.write_text("no")
    (project(cfg, A) / "link.txt").symlink_to(secret)
    (project(cfg, A) / "folder").symlink_to(tmp_path)
    assert "project/link.txt" not in [f["id"] for f in files(r, cfg)["files"]]
    for p in ("project/link.txt", "project/folder/secret.txt"):
        assert (
            go(call(r, "GET", f"/_live/sandbox/files/career/download?path={p}"))[0]
            == 404
        )
        assert (
            go(call(r, "POST", f"/_live/sandbox/files/career/delete?path={p}"))[0]
            == 404
        )
    assert secret.exists()


def test_a_file_over_the_limit_is_not_sent(r, cfg, monkeypatch):  # noqa: F811
    make_files(cfg)
    monkeypatch.setattr(created, "MAX_READ", 2)
    path = "/_live/sandbox/files/career/download?path=project/report.pdf"
    assert go(call(r, "GET", path))[0] == 413


def test_keep_moves_a_working_file_into_project(r, cfg):  # noqa: F811
    make_files(cfg)
    path = "/_live/sandbox/files/career/keep?path=threads/12/scratch/out.txt"
    status, body, _ = go(call(r, "POST", path))
    assert status == 200 and json.loads(body)["id"] == "project/scratch/out.txt"
    assert (project(cfg, A) / "scratch" / "out.txt").read_bytes() == b"hello"
    assert not (work(cfg, A) / "scratch" / "out.txt").exists()
    again = go(call(r, "POST", path))
    assert again[0] == 404


def test_keep_will_not_overwrite_or_take_a_kept_file(r, cfg):  # noqa: F811
    make_files(cfg)
    put(work(cfg, A) / "report.pdf", b"other")
    status, body, _ = go(
        call(r, "POST", "/_live/sandbox/files/career/keep?path=threads/12/report.pdf")
    )
    assert status == 409 and "already has" in json.loads(body)["error"]
    assert (project(cfg, A) / "report.pdf").read_bytes() == b"pdf"
    assert (work(cfg, A) / "report.pdf").exists()
    status, body, _ = go(
        call(r, "POST", "/_live/sandbox/files/career/keep?path=project/report.pdf")
    )
    assert status == 409


def test_delete_removes_one_file(r, cfg):  # noqa: F811
    make_files(cfg)
    status, _, _ = go(
        call(r, "POST", "/_live/sandbox/files/career/delete?path=project/report.pdf")
    )
    assert status == 200 and not (project(cfg, A) / "report.pdf").exists()
    folder = "/_live/sandbox/files/career/delete?path=project/downloads"
    assert go(call(r, "POST", folder))[0] == 404  # a folder is not a file here
    assert (project(cfg, A) / "downloads" / "a.csv").exists()


def test_a_run_holding_the_workspace_blocks_changes_but_not_reads(r, cfg):  # noqa: F811
    make_files(cfg)
    run = SimpleNamespace(
        workspace="career",
        task=SimpleNamespace(done=lambda: False),
        timeout=60,
        started=0,
    )
    r._jobs["run-1"] = run
    read = go(call(r, "GET", "/_live/sandbox/files/career"))[0]
    drop = "/_live/sandbox/files/career/delete?path=project/report.pdf"
    keep = "/_live/sandbox/files/career/keep?path=threads/12/scratch/out.txt"
    assert (read, go(call(r, "POST", drop))[0], go(call(r, "POST", keep))[0]) == (
        200,
        409,
        409,
    )
    assert (project(cfg, A) / "report.pdf").exists()
    assert (work(cfg, A) / "scratch" / "out.txt").exists()
    run.task = SimpleNamespace(done=lambda: True)
    assert go(call(r, "POST", drop))[0] == 200


def test_the_list_says_when_it_left_files_out(r, cfg, monkeypatch):  # noqa: F811
    make_files(cfg)
    monkeypatch.setattr(created, "MAX_LISTED", 2)
    got = files(r, cfg)
    assert got["truncated"] is True and len(got["files"]) == 2


def test_the_machines_route_strips_the_prefix_and_the_paths_still_answer(r, cfg):  # noqa: F811
    make_files(cfg)
    status, body, _ = go(call(r, "GET", "/files/career"))
    assert status == 200 and json.loads(body)["workspace"] == "career"
    download = go(call(r, "GET", "/files/career/download?path=project/report.pdf"))
    assert download[1] == b"pdf"
    assert go(call(r, "GET", "/access/career"))[0] == 200
    assert go(call(r, "GET", "/files/career", key=None))[0] == 302  # an app's page
    assert go(call(r, "GET", "/files/career", key="BAD"))[0] == 403
    assert go(call(r, "OPTIONS", "/files/career", key=None))[0] == 204


def make_app(r, name="groceries", title="Groceries", starter="list"):
    go(r.op_app(A, "create", name, title=title, starter=starter))
    item = {"text": "Oat milk", "done": False}
    go(
        r.op_app(
            A, "do", name, patch=[{"op": "add", "path": "/items/-", "value": item}]
        )
    )


def test_lists_the_apps_beside_the_files_with_their_kind_and_state(r, cfg):  # noqa: F811
    make_app(r)
    got = files(r, cfg)
    assert not any(
        f["id"].startswith(("project/apps", "public/apps")) for f in got["files"]
    )
    [app] = got["apps"]
    assert app["id"] == "apps/groceries"
    assert app["name"] == "groceries"
    assert app["title"] == "Groceries"
    assert app["kind"] == "list"
    assert app["summary"] == "1 of 1 left"
    assert app["modified"] > 0
    assert app["page"].endswith("/career/apps/groceries/")
    assert app["card"].endswith("/_live/apps/career/groceries.png")
    assert app["link"] == app["card"].removesuffix(".png")


def test_an_app_made_without_a_starter_has_no_kind(r, cfg):  # noqa: F811
    put(
        project(cfg, A) / "apps" / "notes" / "page.html",
        b"<script>/*APP*/null</script>",
    )
    go(r.op_app(A, "create", "notes", title="Notes"))
    [app] = files(r, cfg)["apps"]
    assert "kind" not in app


def test_an_app_whose_data_cannot_be_read_is_listed_to_delete(r, cfg):  # noqa: F811
    make_app(r)
    put(project(cfg, A) / "apps" / "groceries" / "data.json", b"not json")
    [app] = files(r, cfg)["apps"]
    assert app["title"] == "groceries" and "error" in app


def test_delete_takes_an_app_whole_and_an_unknown_app_is_404(r, cfg):  # noqa: F811
    make_app(r)
    path = "/_live/sandbox/files/career/delete?path=apps/groceries"
    status, body, _ = go(call(r, "POST", path))
    assert status == 200 and json.loads(body) == {
        "id": "apps/groceries",
        "deleted": True,
    }
    assert not (project(cfg, A) / "apps" / "groceries").exists()
    assert not (cfg.public_root / "career" / "apps" / "groceries").exists()
    assert files(r, cfg)["apps"] == []
    status, _, _ = go(call(r, "POST", path))
    assert status == 404


@pytest.mark.parametrize("action", ["keep", "download"])
def test_an_app_is_not_a_file_to_keep_or_download(r, cfg, action):  # noqa: F811
    make_app(r)
    method = "GET" if action == "download" else "POST"
    path = f"/_live/sandbox/files/career/{action}?path=apps/groceries"
    status, _, _ = go(call(r, method, path))
    assert status == 404
