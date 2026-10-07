import json
import re
from datetime import UTC, datetime

import httpx
import pytest
from research.publish import (
    NAME_RE,
    EmbedError,
    ask_embed,
    embed_document,
    free_file_slug,
    published_stamp,
    report_file,
    save_report_file,
    save_then_publish,
    write_document,
)
from sites.store import Entry

TEXT = report_file(
    "Heat pumps",
    "2026-10-03",
    "How do heat pumps do in winter?",
    "https://example.org/reports/heat-pumps/",
    "## Summary\n\nThey work [1].\n\n## Sources\n\n1. x\n",
)


def test_report_file_heads_the_report_with_its_title_question_and_link():
    assert TEXT.startswith(
        "# Heat pumps\n\n_2026-10-03 · deep research on: How do heat pumps do in winter?_\n"
    )
    assert "Published at https://example.org/reports/heat-pumps/" in TEXT
    assert TEXT.endswith("1. x\n")


def test_save_report_file_writes_slug_md_creating_the_folder_and_replacing_an_older_copy(
    tmp_path,
):
    dir = tmp_path / "research"
    save_report_file(dir, "heat-pumps", "old")
    file = save_report_file(dir, "heat-pumps", TEXT)
    assert file == dir / "heat-pumps.md" and file.read_text() == TEXT


def test_free_file_slug_skips_taken_names(tmp_path):
    dir = tmp_path / "research"
    assert free_file_slug(dir, "!!") == "entry"
    assert (
        free_file_slug(dir, "Heat pumps: Åre, Malmö & Göteborg — a comparison")
        == "heat-pumps-are-malmo-goteborg-a-comparison"
    )
    save_report_file(dir, "heat-pumps", "x")
    save_report_file(dir, "heat-pumps-2", "x")
    assert free_file_slug(dir, "Heat pumps") == "heat-pumps-3"


def file_text(url):
    return report_file("Heat pumps", "2026-10-03", "q", url, "the report")


def test_save_then_publish_saves_the_file_before_publishing_then_adds_the_link(
    tmp_path,
):
    dir = tmp_path / "research"
    seen = []

    def publish():
        seen.append((dir / "heat-pumps.md").read_text())
        return Entry(
            "reports",
            "heat-pumps",
            "Heat pumps",
            "2026-10-03",
            "https://h/research/reports/heat-pumps/",
        )

    out = save_then_publish(dir, "Heat pumps", file_text, publish)
    assert "Not published on the research site." in seen[0]
    assert (
        out["file"] == str(dir / "heat-pumps.md") and out["build"].slug == "heat-pumps"
    )
    assert (
        "Published at https://h/research/reports/heat-pumps/"
        in (dir / "heat-pumps.md").read_text()
    )


def test_save_then_publish_keeps_the_file_when_publishing_fails(tmp_path):
    dir = tmp_path / "research"

    def fail():
        raise RuntimeError("not saved: the site didn't build: zola exploded")

    out = save_then_publish(dir, "T", file_text, fail)
    assert "build" not in out and "zola exploded" in out["publish_error"]
    assert re.search(r"Not published[\s\S]*the report", (dir / "t.md").read_text())
    # And when the file can't be written either, both errors are reported.
    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    both = save_then_publish(blocked, "T", file_text, fail)
    assert both.get("file_error") and both["publish_error"] and "file" not in both


def anythingllm(docs_listed=True, message=None, status=200, listed_after=0):
    """A fake AnythingLLM API; the document shows in the workspace from the
    (listed_after + 1)-th look on, as the native embedder's worker finishes late."""
    calls = []

    def handler(request):
        calls.append(
            (
                request.method,
                request.url.path,
                json.loads(request.content) if request.content else None,
            )
        )
        if request.method == "POST":
            return httpx.Response(status, json={"workspace": {}, "message": message})
        adds = next(body["adds"] for m, _, body in calls if m == "POST")
        looks = sum(1 for m, _, _ in calls if m == "GET")
        shown = docs_listed and looks > listed_after
        documents = [{"docpath": "custom-documents/other.json"}] + (
            [{"docpath": p} for p in adds] if shown else []
        )
        return httpx.Response(
            200, json={"workspace": {"slug": "main", "documents": documents}}
        )

    return calls, httpx.Client(transport=httpx.MockTransport(handler))


def test_write_document_stores_an_anythingllm_document_for_the_embedder(tmp_path):
    docpath = write_document(
        tmp_path,
        "deep-research",
        "heat-pumps",
        "Heat pumps",
        "https://example.org/r/",
        TEXT,
    )
    assert docpath.startswith("deep-research/")
    assert NAME_RE.fullmatch(docpath.split("/")[1])
    doc = json.loads((tmp_path / docpath).read_text())
    assert doc["pageContent"] == TEXT and doc["title"] == "Heat pumps"
    assert doc["chunkSource"] == "link://https://example.org/r/"
    assert doc["wordCount"] > 10 and doc["token_count_estimate"] > 10


def test_embed_document_embeds_it_into_the_workspace(tmp_path):
    calls, client = anythingllm()
    docpath = "deep-research/heat-pumps-x.json"
    assert (
        embed_document("main", docpath, "http://127.0.0.1:3001/api", client) == docpath
    )
    assert calls[0] == (
        "POST",
        "/api/workspace/main/update-embeddings",
        {"adds": [docpath], "deletes": []},
    )
    assert calls[1][:2] == ("GET", "/api/workspace/main")


def test_embed_document_raises_when_the_document_wasnt_embedded():
    args = ("main", "deep-research/s-x.json", "http://127.0.0.1:3001/api")
    with pytest.raises(EmbedError, match="1 documents failed to add"):
        embed_document(
            *args, anythingllm(message="1 documents failed to add.\n\nembedder down")[1]
        )
    with pytest.raises(EmbedError, match="didn't embed it within 0 s"):
        embed_document(*args, anythingllm(docs_listed=False)[1], wait=0.05, poll=0.01)
    with pytest.raises(EmbedError, match="answered 400 for workspace 'main'"):
        embed_document(*args, anythingllm(status=400)[1])


def test_ask_embed_says_why_the_embedder_didnt(tmp_path):
    with pytest.raises(EmbedError, match="the embedder"):
        ask_embed(tmp_path / "missing.sock", "main", "deep-research/s-x.json")


def test_published_stamp_is_like_anythingllms_own():
    assert (
        published_stamp(datetime(2026, 10, 4, 17, 24, 0, tzinfo=UTC))
        == "10/4/2026, 5:24:00 PM"
    )
    assert (
        published_stamp(datetime(2026, 1, 9, 0, 5, 7, tzinfo=UTC))
        == "1/9/2026, 12:05:07 AM"
    )


def test_embed_document_waits_for_the_embedder_to_finish():
    calls, client = anythingllm(listed_after=2)
    embed_document("main", "deep-research/s-x.json", "http://api", client, poll=0.01)
    assert [m for m, _, _ in calls] == ["POST", "GET", "GET", "GET"]


def test_embed_document_logs_in_and_again_after_a_401(tmp_path):
    seen, logins = [], []

    def handler(request):
        seen.append((request.method, request.headers.get("Authorization")))
        if request.headers.get("Authorization") != "Bearer t2":  # t1 has expired
            return httpx.Response(401, text="Unauthorized")
        if request.method == "POST":
            return httpx.Response(200, json={"workspace": {}, "message": None})
        return httpx.Response(
            200,
            json={"workspace": {"documents": [{"docpath": "deep-research/h-x.json"}]}},
        )

    def login(fresh):
        logins.append(fresh)
        return {"Authorization": f"Bearer t{len(logins)}"}

    embed_document(
        "main", "deep-research/h-x.json", "http://127.0.0.1:3001/api",
        httpx.Client(transport=httpx.MockTransport(handler)), login=login,
    )  # fmt: skip
    assert logins == [False, True]
    assert seen == [("POST", "Bearer t1"), ("POST", "Bearer t2"), ("GET", "Bearer t2")]
