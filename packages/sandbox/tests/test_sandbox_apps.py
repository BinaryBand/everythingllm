"""Apps (sandbox.apps): their data, patches, card and page, and the runner's app op."""

import json
import re

import pytest
from sandbox import apps, runner
from sandbox.apps import patch
from sandbox.errors import BadToken, Conflict, StaleToken
from test_runner import (  # noqa: F401 - cfg is a fixture
    A2,
    A,
    B,
    cfg,
    go,
    make,
    project,
    public,
)

PAGE = '<!doctype html><title>Notes</title><script src="app.js"></script><script>App.start(/*APP*/null);</script>'


@pytest.fixture
def r(cfg):  # noqa: F811
    return make(cfg)


def notes(cfg, scope=A, name="notes", page=PAGE):  # noqa: F811
    folder = project(cfg, scope) / "apps" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "page.html").write_text(page)
    return folder


def embedded(page: str) -> dict:
    return json.loads(re.search(r"App\.start\((.*)\);</script>", page)[1])


# --- patches ---


def doc(**data):
    return {"title": "T", "version": 3, "card": {}, "data": data}


def test_a_patch_adds_replaces_moves_and_tests():
    out = patch.apply(
        doc(items=[{"text": "a"}]),
        [
            {"op": "add", "path": "/data/items/-", "value": {"text": "b"}},
            {"op": "add", "path": "/data/items/0", "value": {"text": "z"}},
            {"op": "test", "path": "/data/items/1/text", "value": "a"},
            {"op": "replace", "path": "/data/items/1/text", "value": "A"},
            {"op": "copy", "from": "/data/items/2", "path": "/data/last"},
            {"op": "move", "from": "/data/items/0", "path": "/data/first"},
            {"op": "remove", "path": "/data/items/1"},
            {"op": "replace", "path": "/title", "value": "New"},
            {"op": "add", "path": "/data/a~1b", "value": 1},
        ],
    )
    assert out == {
        "title": "New",
        "card": {},
        "data": {
            "items": [{"text": "A"}],
            "last": {"text": "b"},
            "first": {"text": "z"},
            "a/b": 1,
        },
    }


def test_a_patch_names_the_operation_that_failed():
    with pytest.raises(apps.AppError, match="operation 2 \\(test\\)"):
        patch.apply(
            doc(items=[1]),
            [
                {"op": "add", "path": "/data/items/-", "value": 2},
                {"op": "test", "path": "/data/items/0", "value": 9},
            ],
        )


def test_a_path_outside_title_and_card_is_in_the_data():
    out = patch.apply(
        doc(items=[{"text": "a"}]),
        [
            {"op": "test", "path": "/items/0/text", "value": "a"},
            {"op": "add", "path": "/items/-", "value": {"text": "b"}},
            {"op": "add", "path": "/version", "value": 9},
            {"op": "move", "from": "/items/0", "path": "/data/first"},
            {"op": "replace", "path": "/title", "value": "New"},
        ],
    )
    assert out["title"] == "New"
    assert out["data"] == {
        "items": [{"text": "b"}],
        "version": 9,
        "first": {"text": "a"},
    }
    with pytest.raises(apps.AppError, match="into itself"):
        patch.apply(
            doc(items=[]), [{"op": "move", "from": "/items", "path": "/data/items/x"}]
        )


@pytest.mark.parametrize(
    ("ops", "error"),
    [
        ([], "list of operations"),
        ({"op": "add"}, "list of operations"),
        ([{"op": "merge", "path": "/data"}], "op must be one of"),
        ([{"op": "replace", "path": "", "value": {}}], "JSON pointer"),
        ([{"op": "remove", "path": "/data"}], "can't be removed"),
        ([{"op": "add", "path": "/data/x"}], "needs a value"),
        ([{"op": "remove", "path": "/data/items/5"}], "no item 5"),
        ([{"op": "add", "path": "/data/items/01", "value": 1}], "isn't an index"),
        ([{"op": "replace", "path": "/data/nope", "value": 1}], "nothing there"),
        ([{"op": "add", "path": "/data/nope/x", "value": 1}], "nothing at 'nope'"),
        ([{"op": "move", "from": "/data", "path": "/data/x"}], "into itself"),
        ([{"op": "copy", "from": "/version", "path": "/data/x"}], "nothing there"),
    ],
)
def test_what_a_patch_refuses(ops, error):
    with pytest.raises(apps.AppError, match=error):
        patch.apply(doc(items=[1]), ops)


# --- an app's data, card and page ---


def test_an_apps_data_is_checked_and_its_card_kept_to_what_it_can_draw():
    app = apps.validate(
        {
            "title": " My\tnotes\u200b ",
            "version": "x",
            "card": {"summary": "3 notes", "lines": ["a", "b"], "colour": "red"},
            "data": [1, 2],
        }
    )
    assert app == {
        "title": "My notes",
        "version": 0,
        "card": {"summary": "3 notes", "lines": ["a", "b"]},
        "data": [1, 2],
    }
    for bad, error in [
        ([], "isn't a JSON object"),
        ({"title": "T"}, "has no 'data'"),
        ({"title": "", "data": 1}, "is empty"),
        ({"title": "T", "data": 1, "card": {"lines": ["x"] * 7}}, "at most 6"),
        ({"title": "T", "data": 1, "card": {"items": "/title"}}, "a list in the data"),
        ({"title": "T", "data": 1, "card": {"items": "/data"}}, "a list in the data"),
        ({"title": "T", "data": 1, "card": {"items": "items"}}, "JSON pointer"),
    ]:
        with pytest.raises(apps.AppError, match=error):
            apps.validate(bad)


def test_a_card_counts_rows_from_the_data_or_shows_its_lines():
    app = apps.new("Groceries", "list")
    assert apps.summary(app) == "empty"
    app["data"]["items"] = [
        {"text": "Eggs", "done": True},
        {"text": "Milk", "done": False},
        "Bread",
        7,
    ]
    assert apps.rows(app) == [("Eggs", True), ("Milk", False), ("Bread", None)]
    assert apps.summary(app) == "1 of 2 left"
    app["card"]["summary"] = "Saturday's"
    assert apps.summary(app) == "Saturday's"
    app["card"] = {"items": "/data/missing"}
    assert apps.rows(app) == [] and apps.summary(app) == "empty"
    assert apps.validate({"title": "T", "data": {}, "card": {"items": "/notes"}})[
        "card"
    ] == {"items": "/data/notes"}
    plain = apps.validate({"title": "Notes", "data": {}, "card": {"lines": ["one"]}})
    assert apps.rows(plain) == [("one", None)] and apps.summary(plain) == ""
    for theme in ("light", "dark"):
        assert apps.card(app, theme)[:8] == b"\x89PNG\r\n\x1a\n"
        assert apps.card(plain, theme)[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_page_embeds_the_app_where_no_markup_can_end_the_script():
    app = apps.new("</script><script>alert(1)</script>")
    app["data"] = {"x": "<b>&\u2028"}
    page = apps.render(PAGE, app, "tok", "/_apps/w/n/ops", "/_apps/w/n/sync")
    assert "</script><script>alert" not in page
    assert "\\u003c/script\\u003e" in page and "\\u0026" in page and "\\u2028" in page
    assert embedded(page) == {
        **app,
        "token": "tok",
        "ops": "/_apps/w/n/ops",
        "sync": "/_apps/w/n/sync",
    }
    for bad in ("<p>no place</p>", PAGE + "/*APP*/null"):
        with pytest.raises(apps.AppError, match="exactly once"):
            apps.render(bad, app, "t", "o", "s")


def test_an_old_list_is_moved_to_the_list_starter():
    old = {
        "template": "list",
        "title": "Groceries",
        "items": [
            {"id": 1, "text": "oat milk", "done": False},
            {"id": 2, "text": "eggs", "done": True},
        ],
        "next_id": 3,
        "version": 4,
    }
    assert apps.migrate(old) == {
        "title": "Groceries",
        "version": 4,
        "card": {"items": "/data/items"},
        "data": {
            "items": [
                {"text": "oat milk", "done": False},
                {"text": "eggs", "done": True},
            ]
        },
    }
    assert apps.migrate({"title": "T", "data": {}}) is None
    assert apps.STARTERS["list"]["card"] == {"items": "/data/items"}
    assert apps.starter_page("list").count(apps.PLACEHOLDER.encode()) == 1


# --- the runner's op ---


def test_an_app_from_its_own_page_is_made_changed_shown_and_listed(r, cfg):  # noqa: F811
    with pytest.raises(runner.SandboxError, match="page.html is missing"):
        go(r.op_app(A, "create", "notes"))
    assert not (project(cfg, A) / "apps" / "notes" / "data.json").exists()
    notes(cfg)
    made = go(r.op_app(A, "create", "notes", title="Notes"))
    assert made["did"] == "made it" and made["version"] == 1
    assert made["page"] == "https://ws.example/career/apps/notes/"
    assert made["card"] == (
        "[![Notes](https://pages.example/_live/apps/career/notes.png)]"
        "(https://pages.example/_live/apps/career/notes)"
    )
    saved = json.loads((project(cfg, A) / "apps" / "notes" / "data.json").read_text())
    assert saved == {"title": "Notes", "version": 1, "card": {}, "data": {}}
    out = public(cfg, A) / "apps" / "notes"
    page = embedded((out / "index.html").read_text())
    token, _ = r.app_tokens.held("career", "notes")
    assert page["token"] == token and page["ops"] == "/_apps/career/notes/ops"
    assert page["sync"] == "/_apps/career/notes/sync"
    assert (out / "app.js").read_bytes() == apps.CLIENT.read_bytes()
    assert (cfg.app_state / "career" / "notes.json").stat().st_mode & 0o777 == 0o600

    done = go(
        r.op_app(
            A2,
            "do",
            "notes",
            patch=[{"op": "add", "path": "/data/text", "value": "hello"}],
            version=1,
        )
    )
    assert done["did"] == "applied 1 change" and done["version"] == 2
    assert "token" not in done and "data" not in done
    assert (
        r.app_tokens.held("career", "notes")[0] == token
    )  # the same page, the same token
    with pytest.raises(
        runner.SandboxError, match="changed since version 1.*at version 2"
    ):
        go(
            r.op_app(
                A,
                "do",
                "notes",
                patch=[{"op": "remove", "path": "/data/text"}],
                version=1,
            )
        )

    shown = go(r.op_app(A, "show", "notes"))
    assert shown["version"] == 2 and shown["data"] == {"text": "hello"}
    assert shown["card_spec"] == {}
    (out / "index.html").unlink()
    go(r.op_app(A, "show", "notes"))
    assert (out / "index.html").exists()  # its page comes back, the version as it was
    [listed] = go(r.op_app(A, "list"))["apps"]
    assert (listed["name"], listed["title"], listed["version"]) == ("notes", "Notes", 2)
    assert go(r.op_app(B, "list")) == {"apps": []}  # another workspace's


def test_a_list_from_the_starter(r, cfg):  # noqa: F811
    made = go(r.op_app(A, "create", "groceries", title="Groceries", starter="list"))
    assert made["summary"] == "empty"
    page = project(cfg, A) / "apps" / "groceries" / "page.html"
    assert page.read_bytes() == apps.starter_page("list")
    done = go(
        r.op_app(
            A,
            "do",
            "groceries",
            patch=[
                {
                    "op": "add",
                    "path": "/data/items/-",
                    "value": {"text": "Oat milk", "done": False},
                },
                {
                    "op": "add",
                    "path": "/data/items/-",
                    "value": {"text": "Eggs", "done": True},
                },
            ],
        )
    )
    assert done["summary"] == "1 of 2 left" and done["did"] == "applied 2 changes"
    # A starter never overwrites a page that's there.
    notes(cfg, name="mine")
    go(r.op_app(A, "create", "mine", starter="list"))
    assert (project(cfg, A) / "apps" / "mine" / "page.html").read_text() == PAGE
    with pytest.raises(runner.SandboxError, match="starter must be one of"):
        go(r.op_app(A, "create", "x", starter="kanban"))


def test_a_big_apps_data_goes_to_run_code_not_the_reply(r, cfg, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runner, "APP_SHOW_BYTES", 20)
    notes(cfg)
    go(r.op_app(A, "create", "notes"))
    go(
        r.op_app(
            A,
            "do",
            "notes",
            patch=[{"op": "add", "path": "/data/t", "value": "x" * 50}],
        )
    )
    shown = go(r.op_app(A, "show", "notes"))
    assert "data" not in shown and "/project/apps/notes/data.json" in shown["data_note"]
    monkeypatch.setattr(runner, "APP_DATA_BYTES", 200)
    with pytest.raises(runner.SandboxError, match="would be over"):
        go(
            r.op_app(
                A,
                "do",
                "notes",
                patch=[{"op": "add", "path": "/data/t", "value": "x" * 300}],
            )
        )


def test_an_app_a_run_broke_is_reported_not_rendered(r, cfg):  # noqa: F811
    notes(cfg)
    go(r.op_app(A, "create", "notes"))
    (project(cfg, A) / "apps" / "notes" / "data.json").write_text('{"title": "x"}')
    with pytest.raises(runner.SandboxError, match="notes's data can't be used"):
        go(
            r.op_app(
                A, "do", "notes", patch=[{"op": "add", "path": "/data/x", "value": 1}]
            )
        )
    [listed] = go(r.op_app(A, "list"))["apps"]
    assert "can't be used" in listed["error"]


def test_a_patch_that_breaks_the_app_is_refused(r, cfg):  # noqa: F811
    notes(cfg)
    go(r.op_app(A, "create", "notes"))
    for ops, error in [
        ([{"op": "replace", "path": "/title", "value": ""}], "is empty"),
        ([{"op": "add", "path": "/card/lines", "value": "x"}], "lines must be a list"),
        ([{"op": "add", "path": "", "value": 9}], "JSON pointer"),
    ]:
        with pytest.raises(runner.SandboxError, match=error):
            go(r.op_app(A, "do", "notes", patch=ops))
    assert go(r.op_app(A, "show", "notes"))["version"] == 1


def test_a_symlinked_app_is_never_read(r, cfg, tmp_path):  # noqa: F811
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps(apps.new("Secret")))
    notes(cfg, name="x")
    (project(cfg, A) / "apps" / "x" / "data.json").symlink_to(secret)
    with pytest.raises(runner.SandboxError, match="no app 'x'"):
        go(r.op_app(A, "show", "x"))
    secret_page = tmp_path / "page.html"
    secret_page.write_text(PAGE)
    folder = project(cfg, A) / "apps" / "y"
    folder.mkdir()
    (folder / "page.html").symlink_to(secret_page)
    with pytest.raises(runner.SandboxError, match="page.html is missing"):
        go(r.op_app(A, "create", "y"))


def test_delete_takes_the_page_data_and_token(r, cfg):  # noqa: F811
    go(r.op_app(A, "create", "todo", starter="list"))
    assert go(r.op_app(A, "delete", "todo")) == {"name": "todo", "deleted": True}
    assert not (project(cfg, A) / "apps" / "todo").exists()
    assert not (public(cfg, A) / "apps" / "todo").exists()
    assert not (cfg.app_state / "career" / "todo.json").exists()
    with pytest.raises(runner.SandboxError, match="no app 'todo'"):
        go(r.op_app(A, "delete", "todo"))


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"action": "rename"}, "action must be one of"),
        ({"action": "create", "name": "Bad Name"}, "name must be"),
        (
            {
                "action": "do",
                "name": "nope",
                "patch": [{"op": "remove", "path": "/data/x"}],
            },
            "no app 'nope'",
        ),
    ],
)
def test_what_the_op_refuses(r, args, error):
    with pytest.raises(runner.SandboxError, match=error):
        go(r.op_app(A, **args))


def test_a_second_create_is_refused(r):
    go(r.op_app(A, "create", "todo", starter="list"))
    with pytest.raises(runner.SandboxError, match="already"):
        go(r.op_app(A, "create", "todo", starter="list"))


def test_a_run_that_edits_an_apps_data_or_page_renders_it_again(cfg):  # noqa: F811
    notes(cfg)
    go(make(cfg).op_app(A, "create", "notes"))
    first, _ = make(cfg).app_tokens.held("career", "notes")

    def effect(m):
        folder = m["/project"] / "apps" / "notes"
        data = json.loads((folder / "data.json").read_text())
        data["title"] = "Journal"
        (folder / "data.json").write_text(json.dumps(data))
        (folder / "page.html").write_text(
            PAGE.replace("<title>Notes", "<title>Journal")
        )

    r = make(cfg, effect=effect)
    go(r.op_run(A, "python", "..."))
    page = (public(cfg, A) / "apps" / "notes" / "index.html").read_text()
    assert "<title>Journal" in page and embedded(page)["title"] == "Journal"
    assert go(r.op_app(A, "show", "notes"))["version"] == 2
    now, old = r.app_tokens.held("career", "notes")
    assert now != first and first in old  # a new page, a new token


def test_a_page_changes_the_app_only_with_its_current_token(r, cfg):  # noqa: F811
    notes(cfg)
    go(r.op_app(A, "create", "notes"))
    s = r.scope(A)
    first, _ = r.app_tokens.held("career", "notes")
    add = [{"op": "add", "path": "/data/n", "value": 1}]
    data, _ = r.change_app(s, "notes", add, 1, token=first)
    assert data["data"] == {"n": 1}
    assert r.app_tokens.held("career", "notes")[0] == first  # a save keeps the token
    with pytest.raises(Conflict) as stale:
        r.change_app(s, "notes", add, 1, token=first)
    assert stale.value.app == data
    with pytest.raises(BadToken):
        r.change_app(s, "notes", add, token="forged")
    notes(cfg, page=PAGE + "<!-- changed -->")
    go(r.op_app(A, "show", "notes"))
    with pytest.raises(StaleToken, match="out of date"):
        r.change_app(s, "notes", add, token=first)


def test_the_runner_refreshes_apps_when_it_starts(r, cfg):  # noqa: F811
    folder = project(cfg, A) / "apps" / "groceries"
    folder.mkdir(parents=True)
    (folder / "data.json").write_text(
        json.dumps(
            {
                "template": "list",
                "title": "Groceries",
                "items": [{"id": 1, "text": "oat milk", "done": False}],
                "next_id": 2,
                "version": 1,
            }
        )
    )
    notes(cfg)
    go(r.op_app(A, "create", "notes"))
    old_client = public(cfg, A) / "apps" / "notes" / "app.js"
    old_client.write_text("// an older app.js")
    r.refresh_apps()
    shown = go(r.op_app(A, "show", "groceries"))
    assert shown["version"] == 2 and shown["summary"] == "1 of 1 left"
    assert shown["data"] == {"items": [{"text": "oat milk", "done": False}]}
    assert (folder / "page.html").read_bytes() == apps.starter_page("list")
    assert (public(cfg, A) / "apps" / "groceries" / "app.js").exists()
    # Today's apps get the current client, and nothing else changes.
    assert old_client.read_bytes() == apps.CLIENT.read_bytes()
    assert go(r.op_app(A, "show", "notes"))["version"] == 1


def test_a_card_shows_only_the_rows_its_where_picks():
    app = apps.validate(
        {
            "title": "Reading",
            "data": {
                "books": [
                    {"title": "Dune", "status": "reading"},
                    {"title": "Emma", "status": "want"},
                    {"title": "Ubik", "status": "reading"},
                    "a stray string",
                ]
            },
            "card": {
                "items": "/books",
                "text": "title",
                "where": {"status": "reading"},
            },
        }
    )
    assert app["card"]["items"] == "/data/books"
    assert apps.rows(app) == [("Dune", None), ("Ubik", None)]  # no done key: no box
    assert apps.summary(app) == "2 items"
    for where in ([], {"a": [1]}, {f"k{n}": 1 for n in range(5)}):
        with pytest.raises(apps.AppError, match="where must be"):
            apps.validate(
                {"title": "T", "data": {}, "card": {"items": "/x", "where": where}}
            )


def test_a_change_to_a_list_item_by_its_place_needs_a_test_of_it():
    def loose(*ops):
        return patch.unguarded(list(ops))

    assert loose({"op": "remove", "path": "/items/3"}) == ["/data/items/3"]
    assert loose({"op": "replace", "path": "/data/items/3/done", "value": True}) == [
        "/data/items/3/done"
    ]
    assert loose({"op": "move", "from": "/items/1", "path": "/items/0"}) == [
        "/data/items/1"
    ]
    # Guarded by a test of the item, of something in it, or of the whole list.
    for guard in ("/items/3", "/items/3/text", "/items"):
        assert not loose(
            {"op": "test", "path": guard, "value": "x"},
            {"op": "remove", "path": "/items/3"},
        )
    # Adding by place loses nothing, and neither does copying to a place; paths with no
    # place are what they are.
    assert not loose(
        {"op": "add", "path": "/items/0", "value": 1},
        {"op": "copy", "from": "/title", "path": "/items/2"},
        {"op": "replace", "path": "/title", "value": "T"},
        {"op": "remove", "path": "/notes"},
    )
    # A test of another item guards nothing.
    assert loose(
        {"op": "test", "path": "/items/2/text", "value": "x"},
        {"op": "remove", "path": "/items/3"},
    ) == ["/data/items/3"]


def test_do_refuses_a_change_by_place_without_a_test_or_version(r):
    go(r.op_app(A, "create", "groceries", starter="list"))
    add = {"op": "add", "path": "/items/-", "value": {"text": "Eggs", "done": False}}
    go(r.op_app(A, "do", "groceries", patch=[add, add]))
    tick = {"op": "replace", "path": "/items/1/done", "value": True}
    with pytest.raises(
        runner.SandboxError, match="/data/items/1/done changes a list item"
    ):
        go(r.op_app(A, "do", "groceries", patch=[tick]))
    guarded = [{"op": "test", "path": "/items/1/text", "value": "Eggs"}, tick]
    assert go(r.op_app(A, "do", "groceries", patch=guarded))["summary"] == "1 of 2 left"
    assert go(r.op_app(A, "do", "groceries", patch=[tick], version=3))["version"] == 4


def test_the_data_cant_have_the_apps_own_names_at_its_top():
    for key in ("title", "card", "data"):
        with pytest.raises(apps.AppError, match=f"top-level {key}"):
            apps.validate({"title": "T", "data": {key: 1}})
    assert apps.validate({"title": "T", "data": {"cards": [], "version": 2}})[
        "data"
    ] == {
        "cards": [],
        "version": 2,
    }


def test_write_file_on_an_apps_page_or_data_renders_it_again(r, cfg):  # noqa: F811
    go(r.op_app(A, "create", "notes", starter="blank"))
    page = project(cfg, A) / "apps" / "notes" / "page.html"
    assert page.read_bytes() == apps.starter_page("blank")
    go(r.op_write(A, "/project/apps/notes/page.html", PAGE.replace("Notes", "Journal")))
    shown = (public(cfg, A) / "apps" / "notes" / "index.html").read_text()
    assert "<title>Journal" in shown
    assert go(r.op_app(A, "show", "notes"))["version"] == 2  # rendered, one version on


def test_show_gives_the_card_even_when_the_data_is_too_big_to(r, cfg, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runner, "APP_SHOW_BYTES", 5)
    go(r.op_app(A, "create", "groceries", starter="list"))
    shown = go(r.op_app(A, "show", "groceries"))
    assert shown["card_spec"] == {"items": "/data/items"} and "data" not in shown
