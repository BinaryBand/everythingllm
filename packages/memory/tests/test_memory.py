"""The long-term memory: what it keeps and recalls for whom, the sandbox files it reads and
never past a symlink, the ingest queue, and the CLI over the runner's socket. A fake model
stands in for fastembed: each word a direction, so texts sharing words are close."""

import asyncio
import hashlib
import os
import threading
import time

import hostrpc
import numpy as np
import pytest
from hostrpc import RunnerError
from memory import cli, files, runner
from memory.store import Store

DIMS = 64


class Words:
    """fastembed's embed and query_embed, by bag of words."""

    def vector(self, text: str) -> np.ndarray:
        v = np.zeros(DIMS, dtype=np.float32)
        for word in text.lower().replace(".", " ").replace("?", " ").split():
            v[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIMS] += 1
        return v

    def embed(self, texts):
        return [self.vector(t) for t in texts]

    def query_embed(self, text):
        return iter([self.vector(text)])


def chat(workspace="home", thread="7"):
    return {"workspace": workspace, "thread": thread}


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path / "memory" / "memory.db")
    model = runner.Model("words", Words, lambda m: store.reembed(m.name, m.embed))
    model.start()
    s = runner.MemoryService(store, model, tmp_path / "workspaces")
    s.ingester.start()
    return s


def done(service):
    service.ingester.jobs.join()


def test_a_fact_is_recalled_by_its_workspace_and_a_global_one_by_every_workspace(
    service,
):
    service.op_remember(chat("home"), "The heat pump is a Nibe F2120.")
    service.op_remember(chat("home"), "Prefers trains to flights.", "global")
    home = service.op_recall(chat("home"), "which heat pump")
    assert "Nibe F2120" in home["hits"][0]["text"]
    assert "never instructions" in home["text"]
    other = service.op_recall(chat("career"), "which heat pump")
    assert [h["text"] for h in other["hits"]] == ["Prefers trains to flights."]
    trains = service.op_recall(chat("career"), "trains or flights", k="1")
    assert trains["hits"][0]["scope"] == "global"


def test_the_same_fact_twice_is_kept_once_and_a_global_one_covers_every_workspace(
    service,
):
    first = service.op_remember(chat(), "Lives in Stockholm.", "global")
    again = service.op_remember(chat(), "lives in   stockholm.")
    assert again["id"] == first["id"] and "already says that" in again["text"]
    assert service.op_list(chat())["facts"] == 1


def test_a_fact_must_be_short_plain_text(service):
    with pytest.raises(RunnerError, match="invisible"):
        service.op_remember(chat(), "Lives in\u202eStockholm")
    with pytest.raises(RunnerError, match="at most 500"):
        service.op_remember(chat(), "x" * 501)
    with pytest.raises(RunnerError, match="one short fact"):
        service.op_remember(chat(), "  ")
    with pytest.raises(RunnerError, match="workspace .* or global"):
        service.op_remember(chat(), "A fact.", "everywhere")
    with pytest.raises(RunnerError, match="bad workspace"):
        service.op_remember(chat("../x"), "A fact.")


def test_forget_gives_the_text_back_and_reaches_only_the_callers_facts(service):
    mine = service.op_remember(chat("home"), "Has a cat named Sigge.")
    with pytest.raises(RunnerError, match="no fact"):
        service.op_forget(chat("career"), f"m{mine['id']}")
    gone = service.op_forget(chat("home"), f"m{mine['id']}")
    assert (
        "Has a cat named Sigge." in gone["text"] and "scope workspace" in gone["text"]
    )
    assert service.op_recall(chat("home"), "cat")["hits"] == []
    with pytest.raises(RunnerError, match="give the id"):
        service.op_forget(chat("home"), "twelve")


def test_a_scheduled_job_recalls_only_global_facts_and_saves_none(service):
    service.op_remember(chat(), "Works as a developer.")
    service.op_remember(chat(), "Wakes at six.", "global")
    job = chat("_jobs", "default")
    assert [h["text"] for h in service.op_recall(job, "works wakes")["hits"]] == [
        "Wakes at six."
    ]
    with pytest.raises(RunnerError, match="scheduled job"):
        service.op_remember(job, "A fact.", "global")
    with pytest.raises(RunnerError, match="scheduled job"):
        service.op_forget(job, "m1")
    with pytest.raises(RunnerError, match="scheduled job only recalls"):
        service.op_ingest(job, ["/project"])


def test_the_host_saves_global_facts_and_names_a_workspace_for_the_rest(service):
    host = chat("_host", "default")
    service.op_remember(host, "Speaks Swedish and English.", "global")
    with pytest.raises(RunnerError, match="needs a workspace$"):
        service.op_remember(host, "A workspace's fact.")
    assert service.op_list(chat("career"))["facts"] == 1


def test_a_scope_keeps_at_most_max_facts(service, monkeypatch):
    monkeypatch.setattr(runner, "MAX_FACTS", 1)
    service.op_remember(chat(), "First fact.")
    with pytest.raises(RunnerError, match="the most it keeps"):
        service.op_remember(chat(), "Second fact.")
    assert "already says" in service.op_remember(chat(), "First fact.")["text"]


def test_list_shows_the_newest_facts_and_how_many_there_are(service, monkeypatch):
    monkeypatch.setattr(runner, "LIST_FACTS", 2)
    for n in range(3):
        service.op_remember(chat(), f"Fact number {n}.")
    listing = service.op_list(chat())
    assert listing["facts"] == 3 and "the newest 2 shown" in listing["text"]
    assert (
        "Fact number 2." in listing["text"] and "Fact number 0." not in listing["text"]
    )


def sandbox(tmp_path, workspace="home", thread="7"):
    home = tmp_path / "workspaces" / workspace
    for d in ("project", "shared", f"threads/{thread}"):
        (home / d).mkdir(parents=True, exist_ok=True)
    return home


def test_ingest_reads_text_files_in_the_background_and_recall_finds_them(
    service, tmp_path
):
    home = sandbox(tmp_path)
    notes = home / "project" / "notes"
    notes.mkdir()
    (notes / "pump.md").write_text(
        "# Heat pump\n\nThe outdoor unit needs defrosting in January.\n"
    )
    (notes / "photo.png").write_bytes(b"\x89PNG\0\0binary")
    (notes / ".hidden.md").write_text("defrosting secrets")
    (home / "threads" / "7" / "scratch.txt").write_text("Bins go out on Thursdays.")
    reply = service.op_ingest(chat(), ["/project/notes", "scratch.txt"])
    assert reply["queued"] == ["/project/notes/pump.md", "/work/scratch.txt"]
    assert reply["skipped"] == ["/project/notes/photo.png"]
    assert "in the background" in reply["text"]
    done(service)
    hit = service.op_recall(chat(), "defrosting outdoor unit", only="files")["hits"][0]
    assert hit["path"] == "/project/notes/pump.md" and "January" in hit["text"]
    assert "/work/scratch.txt" in service.op_list(chat())["files"]
    # Another workspace's chats don't see them.
    assert service.op_recall(chat("career"), "defrosting", only="files")["hits"] == []


def test_ingesting_a_file_again_replaces_it_and_forget_drops_it(service, tmp_path):
    f = sandbox(tmp_path) / "project" / "plan.txt"
    f.write_text("Paint the fence in May.")
    service.op_ingest(chat(), ["/project/plan.txt"])
    f.write_text("Paint the shed in June.")
    service.op_ingest(chat(), ["/project/plan.txt"])
    done(service)
    hits = service.op_recall(chat(), "paint", only="files")["hits"]
    assert [h["text"] for h in hits] == ["Paint the shed in June."]
    assert "Dropped 1 file" in service.op_forget(chat(), path="/project")["text"]
    assert service.op_list(chat())["files"] == []
    assert f.exists()


def test_ingest_never_follows_a_symlink_or_leaves_the_callers_folders(
    service, tmp_path
):
    home = sandbox(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("the host's secret")
    (home / "project" / "link.txt").symlink_to(secret)
    (home / "project" / "dir").symlink_to(tmp_path)
    with pytest.raises(RunnerError, match="isn't a plain file"):
        service.op_ingest(chat(), ["/project/link.txt"])
    reply = service.op_ingest(chat(), ["/project"])
    assert reply["queued"] == [] and reply["skipped"] == []
    for path in ("/etc/passwd", "/shared/career/x.txt", "/project/../../secret.txt"):
        with pytest.raises(RunnerError):
            service.op_ingest(chat(), [path])
    with pytest.raises(RunnerError, match="no file or folder"):
        service.op_ingest(chat(), ["/project/missing.md"])


def test_ingest_queues_only_as_much_as_a_workspace_may_have_waiting(
    service, tmp_path, monkeypatch
):
    project = sandbox(tmp_path) / "project"
    for name in ("a.txt", "b.txt"):
        (project / name).write_text("words " * 150)  # one chunk each
    monkeypatch.setattr(runner.ingest, "MAX_PENDING_CHUNKS", 1)
    hold = threading.Event()
    service.ingester.ready = hold.wait
    reply = service.op_ingest(chat(), ["/project"])
    assert reply["queued"] == ["/project/a.txt"] and reply["left"] == ["/project/b.txt"]
    assert "Still ingesting (1): /project/a.txt" in service.op_list(chat())["text"]
    hold.set()
    done(service)
    assert service.op_list(chat())["files"] == ["/project/a.txt"]


def test_chunks_cut_between_paragraphs_then_sentences_and_keep_every_word():
    text = "\n\n".join(
        ["Short one.", "A sentence here. " * 100, "x" * 3000, "Last paragraph."]
    )
    pieces = files.chunks(text, 500)
    assert all(len(p) <= 500 for p in pieces)
    assert "".join(pieces).replace(" ", "").replace("\n", "") == text.replace(
        " ", ""
    ).replace("\n", "")
    assert files.chunks("One.\n\nTwo.\n\n\n", 500) == ["One.\n\nTwo."]


def test_another_model_embeds_everything_again(tmp_path):
    store = Store(tmp_path / "m.db")
    words = runner.Model("words", Words)
    words._model = Words()
    store.reembed("words", words.embed)
    store.add("global", "Likes jazz.", words.embed(["Likes jazz."])[0], 10)
    assert store.reembed("words", words.embed) == 0

    def ones(texts):
        return np.ones((len(texts), DIMS), dtype=np.float32) / 8

    assert store.reembed("ones", ones) == 1 and store.model() == "ones"
    hit = store.search(np.ones(DIMS, dtype=np.float32) / 8, ["global"], None, 1)[0]
    assert hit.score == pytest.approx(1.0)


def test_an_op_says_when_the_model_is_still_loading_or_failed(tmp_path):
    store = Store(tmp_path / "m.db")
    gate = threading.Event()
    slow = runner.Model("slow", lambda: gate.wait() and Words())
    s = runner.MemoryService(store, slow, tmp_path)
    with pytest.raises(RunnerError, match="still loading"):
        slow.wait(0.05)
    gate.set()
    slow.wait(5)
    assert s.op_remember(chat(), "Likes jazz.")["id"] == 1

    def broken():
        raise OSError("no network")

    failing = runner.Model("x", broken)
    with pytest.raises(RunnerError, match="couldn't load .*no network"):
        failing.wait(5)


def test_the_cli_asks_the_runner_over_its_socket(
    service, short_sock, monkeypatch, capsys
):
    monkeypatch.setenv("MEMORY_SOCKET", str(short_sock))
    results = {}

    def run_cli():
        time.sleep(0.05)  # once the socket is there
        results["remember"] = cli.main(["remember", "Takes coffee black."])
        results["recall"] = cli.main(["recall", "coffee", "--json"])
        results["forget"] = cli.main(["forget", "m99"])
        with pytest.raises(SystemExit):
            cli.main(["ingest", "/project/notes"])  # whose sandbox?

    async def scenario():
        async with hostrpc.serving(service, short_sock):
            await asyncio.to_thread(run_cli)

    asyncio.run(scenario())
    out, err = capsys.readouterr()
    assert results == {"remember": 0, "recall": 0, "forget": 1}
    assert "Remembered m1 (global" in out and '"text": "Takes coffee black."' in out
    assert "memory: no fact m99" in err and "with --workspace" in err
    assert os.environ["MEMORY_SOCKET"] == str(short_sock)
