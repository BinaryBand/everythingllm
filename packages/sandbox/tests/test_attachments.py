"""A chat's attachments in /work/attachments: the run-code skill names them, and the runner
copies their text from AnythingLLM's uploads folder before the run."""

import json
import os
import unicodedata
from pathlib import Path

import pytest
from sandbox import attachments, workspace
from sandbox.runner import Runner
from sandbox.workspace import Config
from test_runner import A2, A, FakePodman, cfg, go, make, project, work  # noqa: F401

CLIENT = {"workspace": "client-acme", "thread": "gateway", "gateway": True}


@pytest.fixture
def uploads(cfg, tmp_path):  # noqa: F811
    cfg.uploads = tmp_path / "uploads"
    cfg.uploads.mkdir()
    return cfg.uploads


def upload(folder: Path, file: str, title: str, text: str) -> dict:
    """An attachment as AnythingLLM keeps it, and as the skill names it."""
    (folder / file).write_text(json.dumps({"title": title, "pageContent": text}))
    return {"title": title, "file": file}


def attached(config, scope=A) -> Path:
    return work(config, scope) / "attachments"


def manifest(config, scope=A) -> dict:
    return json.loads((attached(config, scope) / ".manifest.json").read_text())


def run(r, scope=A, attachments=None, known=True, **kw):
    return go(
        r.op_run(
            scope,
            "bash",
            "ls",
            attachments=attachments,
            attachments_known=known,
            **kw,
        )
    )


def test_attachments_are_in_work_as_text_before_the_run_and_arent_changed_files(
    cfg,  # noqa: F811
    uploads,
):
    files = [
        upload(uploads, "data.csv-1a.json", "data.csv", "a,b\n1,2\n"),
        upload(uploads, "q3-report.pdf-2b.json", "Q3 report.pdf", "Revenue grew."),
        upload(
            uploads,
            "book.xlsx-3c.json",
            "book.xlsx (Sheet: Sales)",
            "\nSheet: Sales\nx,y\n",
        ),
    ]
    seen = {}

    def effect(m):
        folder = m["/work"] / "attachments"
        seen.update({p.name: p.read_text() for p in folder.iterdir()})

    r = make(cfg, effect=effect)
    res = run(r, attachments=files)
    assert res["attachments"] == [
        "Q3_report.pdf.txt",
        "book.xlsx_Sheet_Sales.txt",
        "data.csv",
    ]
    assert res["attachment_notes"] == [] and res["changed"] == []
    assert seen["data.csv"] == "a,b\n1,2\n"
    assert seen["Q3_report.pdf.txt"] == "Revenue grew."
    assert seen["book.xlsx_Sheet_Sales.txt"] == "\nSheet: Sales\nx,y\n"
    entry = manifest(cfg)["data.csv"]
    assert entry["source"] == "data.csv-1a.json" and entry["bytes"] == 8
    assert len(entry["sha256"]) == 64
    # Each chat has its own: another thread of the workspace has none of them.
    r.podman.effect = None
    assert run(r, A2, attachments=[])["attachments"] == []
    assert not attached(cfg, A2).exists()


def test_a_copy_is_written_once_and_an_edited_one_is_the_chats_own(
    cfg,  # noqa: F811
    uploads,
):
    files = [
        upload(uploads, "data.csv-1a.json", "data.csv", "a,b\n"),
        upload(uploads, "notes.md-2b.json", "notes.md", "# notes\n"),
    ]
    r = make(cfg)
    run(r, attachments=files)
    (attached(cfg) / "data.csv").write_text("a,b\nmine\n")
    (attached(cfg) / "notes.md").unlink()  # deleted copies come back while attached
    res = run(r, attachments=files)
    assert res["attachments"] == ["data.csv", "notes.md"]
    assert (attached(cfg) / "data.csv").read_text() == "a,b\nmine\n"
    assert (attached(cfg) / "notes.md").read_text() == "# notes\n"
    # Both detached: the unchanged copy goes, the edited one stays, no longer an attachment.
    res = run(r, attachments=[])
    assert res["attachments"] == [] and res["attachment_notes"] == []
    assert sorted(os.listdir(attached(cfg))) == ["data.csv"]
    assert (attached(cfg) / "data.csv").read_text() == "a,b\nmine\n"
    # Attached again, it's copied beside the chat's own file, which keeps its name.
    assert run(r, attachments=files[:1])["attachments"] == ["data-2.csv"]


def test_copies_go_only_when_the_lookup_was_whole(cfg, uploads):  # noqa: F811
    files = [upload(uploads, "data.csv-1a.json", "data.csv", "a,b\n")]
    r = make(cfg)
    run(r, attachments=files)
    # A skill whose lookup failed sends no attachments_known; an older one, nothing at all.
    run(r, attachments=[], known=False)
    go(r.op_run(A, "bash", "ls"))
    assert (attached(cfg) / "data.csv").is_file()
    run(r, attachments=[])
    assert not attached(cfg).exists()  # emptied, so removed


def test_a_source_thats_gone_is_skipped_and_its_copy_kept(cfg, uploads):  # noqa: F811
    files = [upload(uploads, "data.csv-1a.json", "data.csv", "a,b\n")]
    r = make(cfg)
    run(r, attachments=files)
    (uploads / "data.csv-1a.json").unlink()
    res = run(r, attachments=files)
    assert res["attachments"] == ["data.csv"]  # the copy is still there
    (attached(cfg) / "data.csv").unlink()
    res = run(r, attachments=files)
    assert res["attachments"] == []
    assert res["attachment_notes"] == [
        "data.csv is no longer on the server, so it wasn't copied"
    ]
    assert "data.csv" in manifest(cfg)  # still attached, so not forgotten


@pytest.mark.parametrize(
    "file",
    [
        "../secret.json",
        "a/b.json",
        "/etc/x.json",
        ".hidden.json",
        "notes.txt",
        "",
        None,
        "x" * 300 + ".json",
    ],
)
def test_a_file_name_that_isnt_one_upload_is_refused(cfg, uploads, tmp_path, file):  # noqa: F811
    (tmp_path / "secret.json").write_text(json.dumps({"pageContent": "secret"}))
    res = run(make(cfg), attachments=[{"title": "x.csv", "file": file}, "junk"])
    assert res["attachments"] == []
    assert (
        res["attachment_notes"]
        == ["an attachment with a bad file name was skipped"] * 2
    )


def test_sources_are_read_without_following_a_symlink(cfg, uploads, tmp_path):  # noqa: F811
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"title": "s", "pageContent": "secret"}))
    (uploads / "link-1a.json").symlink_to(secret)
    res = run(make(cfg), attachments=[{"title": "link.csv", "file": "link-1a.json"}])
    assert res["attachments"] == []
    assert res["attachment_notes"][0].startswith("link.csv couldn't be read")
    # Nor is an uploads folder that's a link.
    cfg.uploads = tmp_path / "uploads-link"
    cfg.uploads.symlink_to(uploads)
    upload(uploads, "data.csv-1a.json", "data.csv", "a,b\n")
    res = run(make(cfg), attachments=[{"title": "d.csv", "file": "data.csv-1a.json"}])
    assert res["attachments"] == []
    assert "couldn't be read" in res["attachment_notes"][0]


def test_nothing_is_written_through_a_link_a_run_left_in_work(
    cfg,  # noqa: F811
    uploads,
    tmp_path,
):
    files = [upload(uploads, "data.csv-1a.json", "data.csv", "a,b\n")]
    outside = tmp_path / "outside"
    outside.mkdir()
    r = make(cfg)
    go(r.op_run(A, "bash", "true"))  # the thread's folders exist
    attached(cfg).symlink_to(outside)
    res = run(r, attachments=files)
    assert res["attachments"] == [] and os.listdir(outside) == []
    assert "isn't a folder" in res["attachment_notes"][0]
    attached(cfg).unlink()
    # A link at a copy's name, or a manifest that names a path, leads nowhere either.
    attached(cfg).mkdir()
    (attached(cfg) / "data.csv").symlink_to(outside / "planted.csv")
    (project(cfg, A) / "keep.txt").write_text("kept")
    forged = {"source": "x-1.json", "sha256": "0" * 64, "bytes": 4}
    (attached(cfg) / ".manifest.json").write_text(
        json.dumps({"../../../project/keep.txt": forged, "data.csv": forged})
    )
    res = run(r, attachments=files)
    assert res["attachments"] == ["data-2.csv"]
    assert not (outside / "planted.csv").exists()
    assert (project(cfg, A) / "keep.txt").read_text() == "kept"
    assert (attached(cfg) / "data.csv").is_symlink()  # not ours to remove: it's changed
    assert set(manifest(cfg)) == {"data-2.csv"}


def test_the_caps(cfg, uploads, monkeypatch):  # noqa: F811
    monkeypatch.setattr(attachments, "ATTACHMENT_BYTES", 100)
    monkeypatch.setattr(attachments, "ATTACHMENTS_BYTES", 150)
    files = [
        upload(uploads, "big-1.json", "big.csv", "x" * 100),
        upload(uploads, "a-2.json", "a.csv", "a" * 40),
        upload(uploads, "b-3.json", "b.csv", "b" * 40),
    ]
    res = run(make(cfg), attachments=files)
    assert res["attachments"] == ["a.csv"]
    assert res["attachment_notes"] == [
        "big.csv is over 0 MB, so it wasn't copied",
        (
            "b.csv would take this run over the 0 MB of attachments it copies, so it "
            "wasn't copied"
        ),
    ]
    # Past ATTACHMENTS_MAX, the list isn't whole, so nothing is removed for being left out.
    monkeypatch.setattr(attachments, "ATTACHMENTS_MAX", 1)
    res = run(make(cfg), attachments=[files[2], files[1]])
    assert res["attachment_notes"][0] == "only the first 1 attachments were copied"
    assert (attached(cfg) / "a.csv").is_file()


def test_attachments_past_the_workspaces_limit_are_left_out(
    cfg,  # noqa: F811
    uploads,
    monkeypatch,
):
    monkeypatch.setattr(workspace, "WORKSPACE_MAX_BYTES", 1000)
    files = [
        upload(uploads, "a-1.json", "a.csv", "a" * 600),
        upload(uploads, "b-2.json", "b.csv", "b" * 600),
    ]
    res = run(make(cfg), attachments=files)
    assert res["exit_code"] == 0 and res["attachments"] == ["a.csv"]
    assert res["attachment_notes"] == [
        "b.csv wasn't copied: the workspace's sandbox is near its 0 MB limit"
    ]


def test_a_gateway_clients_scope_gets_no_attachments(cfg, uploads):  # noqa: F811
    files = [upload(uploads, "data.csv-1a.json", "data.csv", "a,b\n")]
    res = run(make(cfg), CLIENT, attachments=files)
    assert res["attachments"] == [] and res["attachment_notes"] == []
    assert not attached(cfg, CLIENT).exists()


def test_a_call_from_an_older_or_newer_skill_still_runs(cfg):  # noqa: F811
    """op_run takes a call without attachments (an older run-code), and ignores arguments
    it doesn't know (a newer one's), so the skill and the runner update in either order."""
    r = make(cfg)
    for args in (
        {"scope": A, "language": "bash", "code": "ls", "timeout": 60},
        {"scope": A, "language": "bash", "code": "ls", "something_new": [1]},
    ):
        reply = go(r.reply({"op": "run", "args": args}))
        assert reply["ok"], reply
        assert reply["result"]["exit_code"] == 0
        assert reply["result"]["attachments"] == []
    assert len(r.podman.runs()) == 2


@pytest.mark.parametrize(
    ("title", "taken", "name"),
    [
        ("data.csv", set(), "data.csv"),
        ("data.csv", {"data.csv", "data-2.csv"}, "data-3.csv"),
        ("Sales 2026.TSV", set(), "Sales_2026.TSV"),
        ("Q3 report.pdf", set(), "Q3_report.pdf.txt"),
        ("book.xlsx (Sheets: Sales, Costs)", set(), "book.xlsx_Sheets_Sales_Costs.txt"),
        ("notes", set(), "notes.txt"),
        (".bashrc", set(), "bashrc.txt"),
        ("../../etc/passwd", set(), "etc_passwd.txt"),
        ("...", set(), "attachment.txt"),
        ("", set(), "attachment.txt"),
        ("rapport $(rm -rf ~).md", set(), "rapport_rm_-rf.md"),
        (unicodedata.normalize("NFD", "café.csv"), set(), "café.csv"),
    ],
)
def test_attachment_names(title, taken, name):
    assert attachments.attachment_name(title, taken) == name


def test_a_long_title_makes_a_name_of_at_most_100_characters():
    taken = set()
    for _ in range(12):
        name = attachments.attachment_name("x" * 300 + ".csv", taken)
        assert len(name) <= attachments.NAME_MAX and name.endswith(".csv")
        taken.add(name)
    assert len(taken) == 12


def test_config_reads_attachments_from_anythingllms_uploads(monkeypatch):
    monkeypatch.delenv("SANDBOX_UPLOADS", raising=False)
    monkeypatch.setenv("ANYTHINGLLM_STORAGE", "/data/allm")
    monkeypatch.setenv("PUBLIC_HOST", "box.tail.ts.net")
    assert Config.from_env().uploads == Path("/data/allm/direct-uploads")
    monkeypatch.setenv("SANDBOX_UPLOADS", "/elsewhere")
    assert Config.from_env().uploads == Path("/elsewhere")


def test_a_runner_without_an_uploads_folder_still_runs(cfg):  # noqa: F811
    r = Runner(cfg, podman=FakePodman())
    res = run(r, attachments=[{"title": "data.csv", "file": "data.csv-1a.json"}])
    assert res["exit_code"] == 0 and res["attachments"] == []
    assert res["attachment_notes"] == [
        "data.csv is no longer on the server, so it wasn't copied"
    ]
