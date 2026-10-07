"""The embedder research-runner asks to add a report to a workspace: only a deep-research
document it left in documents/deep-research-incoming/, moved to where no container can
write before AnythingLLM is asked, never through a symlink."""

import pytest
from audit import embed
from hostrpc import RunnerError

NAME = "heat-pumps-0b1c8a52-3f7e-4d2a-9c1e-5a6b7c8d9e0f.json"


@pytest.fixture
def embedder(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        embed,
        "embed_document",
        lambda workspace, docpath, api, login: calls.append((workspace, docpath)),
    )
    (tmp_path / "documents" / "deep-research-incoming").mkdir(parents=True)
    e = embed.Embedder(tmp_path, "http://127.0.0.1:3001/api", tmp_path / ".env")
    e.calls = calls
    return e


def incoming(tmp_path):
    return tmp_path / "documents" / "deep-research-incoming"


def test_a_report_is_moved_out_of_the_containers_reach_then_embedded(
    embedder, tmp_path
):
    (incoming(tmp_path) / NAME).write_text('{"pageContent": "x"}')
    done = embedder.op_embed_report("career", f"deep-research-incoming/{NAME}")
    assert done == {"docpath": f"deep-research/{NAME}"}
    assert embedder.calls == [("career", f"deep-research/{NAME}")]
    moved = tmp_path / "documents" / "deep-research" / NAME
    assert moved.read_text() == '{"pageContent": "x"}' and not moved.is_symlink()
    assert not (incoming(tmp_path) / NAME).exists()


@pytest.mark.parametrize(
    "workspace, docpath",
    [
        ("Career", f"deep-research-incoming/{NAME}"),
        ("../x", f"deep-research-incoming/{NAME}"),
        ("career", f"deep-research/{NAME}"),
        ("career", f"custom-documents/{NAME}"),
        ("career", f"deep-research-incoming/../custom-documents/{NAME}"),
        ("career", "deep-research-incoming/secret.json"),
        ("career", "deep-research-incoming/"),
    ],
)
def test_only_a_deep_research_document_is_embedded(embedder, workspace, docpath):
    with pytest.raises(RunnerError):
        embedder.op_embed_report(workspace, docpath)
    assert embedder.calls == []


def test_a_symlink_to_another_document_is_refused(embedder, tmp_path):
    private = tmp_path / "documents" / "custom-documents"
    private.mkdir()
    (private / "salary.json").write_text('{"pageContent": "private"}')
    (incoming(tmp_path) / NAME).symlink_to(private / "salary.json")
    with pytest.raises(RunnerError, match="no document"):
        embedder.op_embed_report("career", f"deep-research-incoming/{NAME}")
    assert embedder.calls == []
    assert not (tmp_path / "documents" / "deep-research" / NAME).exists()


def test_a_symlinked_incoming_folder_is_refused(embedder, tmp_path):
    incoming(tmp_path).rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / NAME).write_text("{}")
    incoming(tmp_path).symlink_to(elsewhere)
    with pytest.raises(RunnerError, match="couldn't move"):
        embedder.op_embed_report("career", f"deep-research-incoming/{NAME}")
    assert embedder.calls == []


def test_a_name_already_embedded_isnt_replaced(embedder, tmp_path):
    (tmp_path / "documents" / "deep-research").mkdir()
    (tmp_path / "documents" / "deep-research" / NAME).write_text("first")
    (incoming(tmp_path) / NAME).write_text("second")
    with pytest.raises(RunnerError, match="there already"):
        embedder.op_embed_report("career", f"deep-research-incoming/{NAME}")
    assert (tmp_path / "documents" / "deep-research" / NAME).read_text() == "first"
