"""App templates (sandbox.apps) and the runner's app op."""

import json
import re

import pytest
from sandbox import apps, runner
from sandbox.apps.list import template as lists
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

GATEWAY = {"workspace": "client-laptop", "thread": "gateway", "gateway": True}


@pytest.fixture
def r(cfg, tmp_path):  # noqa: F811
    cfg.app_state = tmp_path / "data" / "apps"
    return make(cfg)


# --- the list template ---


def test_a_list_adds_ticks_and_clears_by_text_or_id():
    d = lists.new("Groceries")
    d, did = lists.apply(d, "add", {"items": ["Oat milk", "Eggs", "oat  milk"]})
    assert did == "added Oat milk, Eggs" and lists.summary(d) == "2 of 2 left"
    d, did = lists.apply(d, "add", {"item": "OAT MILK"})
    assert did == "it was on the list already"
    d, did = lists.apply(d, "check", {"item": "eggs"})
    assert did == "ticked off Eggs" and lists.summary(d) == "1 of 2 left"
    d, _ = lists.apply(d, "add", {"item": "Eggs"})  # bought again: a new, open one
    d, did = lists.apply(
        d, "uncheck", {"item": "Eggs"}
    )  # the done one, not the new one
    assert [i["done"] for i in d["items"]] == [False, False, False]
    d, _ = lists.apply(d, "check", {"item": 1})
    d, did = lists.apply(d, "clear_done", {})
    assert did == "cleared 1 done" and [i["text"] for i in d["items"]] == [
        "Eggs",
        "Eggs",
    ]
    d, did = lists.apply(d, "rename", {"title": "Weekend groceries"})
    assert d["title"] == "Weekend groceries"
    assert [i["id"] for i in d["items"]] == [2, 3] and d["next_id"] == 4


@pytest.mark.parametrize(
    ("op", "args", "error"),
    [
        ("shuffle", {}, "op must be one of"),
        ("add", {}, "add takes item"),
        ("add", {"item": "  "}, "is empty"),
        ("add", {"item": "x" * 201}, "over 200 characters"),
        ("check", {"item": "caviar"}, "no 'caviar' on the list"),
        ("remove", {"item": 99}, "no item 99"),
        ("rename", {"title": 5}, "must be text"),
    ],
)
def test_what_a_list_refuses(op, args, error):
    with pytest.raises(apps.AppError, match=error):
        lists.apply(lists.new("L"), op, args)


def test_a_full_list_takes_no_more():
    d = lists.new("L")
    d, _ = lists.apply(d, "add", {"items": [f"i{n}" for n in range(lists.MAX_ITEMS)]})
    with pytest.raises(apps.AppError, match="full"):
        lists.apply(d, "add", {"item": "one more"})


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"template": "notes"},
        {"template": "list", "title": "L", "items": "milk"},
        {"template": "list", "title": "L", "items": [{"text": "no id"}]},
        {
            "template": "list",
            "title": "L",
            "items": [{"id": 1, "text": "a"}, {"id": 1, "text": "b"}],
        },
        {"template": "list", "title": "", "items": []},
    ],
)
def test_data_a_run_broke_is_refused(data):
    with pytest.raises(apps.AppError):
        apps.of(data).validate(data) if isinstance(data, dict) and data.get(
            "template"
        ) == "list" else apps.of(data)


def test_text_is_one_clean_line():
    d, _ = lists.apply(lists.new("L"), "add", {"item": "Oat\u200b milk\n\tplease\x00"})
    assert d["items"][0]["text"] == "Oat milk please"


def test_the_page_embeds_its_data_where_no_markup_can_end_the_script():
    d, _ = lists.apply(
        lists.new("</script><script>alert(1)</script>"), "add", {"item": "<b>&"}
    )
    page = apps.render(d, "tok", "/_apps/w/n/ops")
    script = page[page.index("const APP = ") :]
    assert "</script><script>" not in script.split("\n", 1)[0]
    assert "\\u003c/script\\u003e" in page and "\\u0026" in page
    assert page.count("/*APP*/") == 0
    embedded = json.loads(re.search(r"const APP = (.*);\n", page)[1])
    assert embedded == {"data": d, "token": "tok", "ops": "/_apps/w/n/ops"}


def test_the_card_draws_in_both_themes():
    d, _ = lists.apply(
        lists.new("Groceries"), "add", {"items": [f"i{n}" for n in range(9)]}
    )
    for theme in ("light", "dark"):
        assert lists.card(d, theme)[:8] == b"\x89PNG\r\n\x1a\n"
    assert lists.card(lists.new("Empty"), "dark")[:4] == b"\x89PNG"


# --- the runner's op ---


def test_an_app_is_made_changed_shown_and_listed(r, cfg):  # noqa: F811
    made = go(
        r.op_app(
            A,
            "create",
            "groceries",
            title="Groceries",
            args={"items": ["Oat milk", "Eggs"]},
        )
    )
    assert made["did"] == "made it and added Oat milk, Eggs"
    assert made["summary"] == "2 of 2 left" and made["version"] == 1
    assert made["page"] == "https://ws.example/career/apps/groceries/"
    assert made["card"] == (
        "[![Groceries](https://pages.example/_live/apps/career/groceries.png)]"
        "(https://pages.example/_live/apps/career/groceries)"
    )
    data = json.loads(
        (project(cfg, A) / "apps" / "groceries" / "data.json").read_text()
    )
    assert data["version"] == 1 and len(data["items"]) == 2
    page = (public(cfg, A) / "apps" / "groceries" / "index.html").read_text()
    assert '"ops": "/_apps/career/groceries/ops"' in page
    token = r.tokens("career", "groceries")["token"]
    assert token and token in page
    assert (cfg.app_state / "career" / "groceries.json").stat().st_mode & 0o777 == 0o600

    done = go(r.op_app(A2, "do", "groceries", op="check", args={"item": "eggs"}))
    assert done["did"] == "ticked off Eggs" and done["summary"] == "1 of 2 left"
    assert "token" not in done and "data" not in done  # the page's, not the agent's
    assert r.tokens("career", "groceries")["token"] != token  # rotated with the render
    assert token in r.tokens("career", "groceries")["old"]

    assert go(r.op_app(A, "show", "groceries"))["version"] == 2
    (public(cfg, A) / "apps" / "groceries" / "index.html").unlink()
    assert go(r.op_app(A, "show", "groceries"))["version"] == 3  # its page comes back
    [listed] = go(r.op_app(A, "list"))["apps"]
    assert (listed["name"], listed["title"]) == ("groceries", "Groceries")
    assert go(r.op_app(B, "list")) == {"apps": []}  # another workspace's


def test_an_app_a_run_broke_is_reported_not_rendered(r, cfg):  # noqa: F811
    go(r.op_app(A, "create", "todo"))
    (project(cfg, A) / "apps" / "todo" / "data.json").write_text('{"template": "list"}')
    with pytest.raises(runner.SandboxError, match="todo's data can't be used"):
        go(r.op_app(A, "do", "todo", op="add", args={"item": "x"}))
    [listed] = go(r.op_app(A, "list"))["apps"]
    assert "can't be used" in listed["error"]


def test_a_symlinked_app_is_never_read(r, cfg, tmp_path):  # noqa: F811
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps(lists.new("Secret")))
    (project(cfg, A) / "apps" / "x").mkdir(parents=True)
    (project(cfg, A) / "apps" / "x" / "data.json").symlink_to(secret)
    with pytest.raises(runner.SandboxError, match="no app 'x'"):
        go(r.op_app(A, "show", "x"))


def test_delete_takes_the_data_page_and_token(r, cfg):  # noqa: F811
    go(r.op_app(A, "create", "todo"))
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
            {"action": "create", "name": "x", "template": "kanban"},
            "template must be one of",
        ),
        ({"action": "do", "name": "nope", "op": "add"}, "no app 'nope'"),
    ],
)
def test_what_the_op_refuses(r, args, error):
    with pytest.raises(runner.SandboxError, match=error):
        go(r.op_app(A, **args))


def test_a_second_create_and_a_gateway_client_are_refused(r):
    go(r.op_app(A, "create", "todo"))
    with pytest.raises(runner.SandboxError, match="already"):
        go(r.op_app(A, "create", "todo"))
    with pytest.raises(runner.SandboxError, match="gateway client"):
        go(r.op_app(GATEWAY, "list"))


def test_a_page_changes_the_app_only_with_its_current_token(r, cfg):  # noqa: F811
    go(r.op_app(A, "create", "todo", args={"item": "a"}))
    s = r.scope(A)
    first = r.tokens("career", "todo")["token"]
    out = r.change_app(s, "todo", "check", {"item": 1}, token=first)
    assert out["data"]["items"][0]["done"] and out["token"] != first
    with pytest.raises(runner.StaleToken, match="out of date"):
        r.change_app(s, "todo", "uncheck", {"item": 1}, token=first)
    with pytest.raises(runner.BadToken):
        r.change_app(s, "todo", "uncheck", {"item": 1}, token="forged")
    assert (
        r.change_app(s, "todo", "uncheck", {"item": 1}, token=out["token"])["version"]
        == 3
    )
