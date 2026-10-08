"""browser-runner with podman faked (browser_fakes): a workspace's browser is started on its
first call, keeps to the workspace's own folders and an address of its own, gives each
thread a tab with a live card, refuses the agent while the user has the browser, and is
stopped when it's idle."""

import asyncio

import pytest
from browser import containers, logins, tabs
from browser import runner as runner_mod
from browser.config import check_scope
from browser.page import UNTRUSTED
from browser.runner import Runner
from browser.vault import VaultError
from browser_fakes import Clock, FakePodman, as_given, config, passkey, scope
from hostrpc import RunnerError


def run(test):
    """Run `test(runner, podman, clock)` against a fresh runner, then clean up."""

    def go(tmp_path, **kw):
        async def main():
            podman, clock = FakePodman(), Clock()
            r = Runner(config(tmp_path, **kw), podman=podman, now=clock)
            try:
                return await test(r, podman, clock)
            finally:
                await podman.close()

        return asyncio.run(main())

    return go


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "career",
        {"workspace": "career"},
        {"workspace": "Career", "thread": "1"},
        {"workspace": "career\n", "thread": "1"},
        {"workspace": "career", "thread": "1\n"},
        {"workspace": "../x", "thread": "1"},
        {"workspace": "client-claude", "thread": "gateway"},
        {"workspace": "career", "thread": "1", "gateway": True},
    ],
)
def test_a_scope_is_a_workspace_and_thread_and_never_a_gateway_clients(bad):
    with pytest.raises(RunnerError):
        check_scope(bad)
    assert check_scope({"workspace": "_jobs", "thread": "default"}) == (
        "_jobs",
        "default",
    )


def test_a_browser_is_hardened_and_mounts_only_its_workspaces_profile_downloads_and_sockets(
    tmp_path,
):
    r = Runner(config(tmp_path))
    s = containers.Session(
        "career",
        "browser-1",
        "10.89.79.32",
        "t",
        tmp_path / "data/sockets/browser-1",
        0,
    )
    args = r.container_args(s)
    joined = " ".join(args)
    for flag in (
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--userns keep-id",
        "--network egress-net:ip=10.89.79.32",
        "--dns none",
        "--memory 2g",
        "--pids-limit 1024",
        "--init",
        "--rm",
    ):
        assert flag in joined, flag
    for flag in (
        "--privileged",
        "--cap-add",
        "-p",
        "--publish",
        "--pid",
        "--ipc",
        "--device",
    ):
        assert flag not in args, flag
    assert "host" not in joined.split("--network ")[1].split()[0]
    assert f"BROWSER_PROXY={r.config.proxy}" in args
    volumes = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    data = "rw,noexec,nosuid,nodev"
    assert volumes == [
        "/repo:/repo:ro",
        f"{tmp_path}/data/profiles/career:/profile:{data}",
        f"{tmp_path}/data/downloads/career:/downloads:{data}",
        f"{tmp_path}/data/sockets/browser-1:/run/browser:{data}",
    ]
    assert args[-1] == containers.IMAGE


def test_open_starts_the_workspaces_browser_and_gives_each_thread_a_tab_and_card(
    tmp_path,
):
    @run
    async def test(r, podman, clock):
        first = await r.op_open(scope(thread="7"), "example.com")
        assert first["new"] and UNTRUSTED in first["page"]
        assert (
            "Address: example.com" in first["page"]
            and '[e1] button "Sign in"' in first["page"]
        )
        assert first["card"].startswith(
            "[![Browser: Title of example.com](https://host.example.ts.net:8445/_live/browser/bw-"
        )
        again = await r.op_open(scope(thread="7"), "example.org")
        link = lambda card: card.split("](", 1)[1]
        assert not again["new"] and link(again["card"]) == link(first["card"])
        assert (
            "Browser: Title of example.org" in again["card"]
        )  # the card says where it is now
        other = await r.op_open(scope(thread="8"), "example.net")
        assert other["new"] and link(other["card"]) != link(first["card"])
        assert (
            len(podman.runs()) == 1
        )  # one browser for the workspace, a tab per thread
        assert (tmp_path / "data" / "profiles" / "career").is_dir()
        assert (tmp_path / "data" / "downloads" / "career").is_dir()
        # The sandbox's folders, which the browser uses only for downloads.
        assert not any((tmp_path / "workspaces").iterdir())
        acted = await r.op_act(scope(thread="7"), "click", "e1")
        assert "Sign in" in acted["page"]
        found = await r.op_read(scope(thread="7"), "home")
        assert (
            '[e2] link "Home"' in found["page"]
            and "Sign in to go on" not in found["page"]
        )
        tab = r.threads[("career", "7")]
        assert tab.last == "Clicked Sign in" and tab.url == "example.org"
        assert await r.op_label(scope(thread="7"), " e2 ") == {"label": "Home"}
        assert await r.op_label(scope(thread="7"), "e9") == {"label": ""}
        assert await r.op_label(scope(thread="9"), "e1") == {"label": ""}  # no tab

    test(tmp_path)


def test_an_elements_label_is_its_name_in_the_last_view():
    view = {
        "elements": [
            '[e1] button "Sign in"',
            '[e2] link "Home" -> /',
            '[e3] input[text] "Customer name" value="Ada"',
            '[e4] input[checkbox] "Say "yes"" (checked)',
            '[e5] input[password] "" (filled)',
            '[e6] select "Size" options: *Small | Large',
            '[e7] textarea "Notes" placeholder="Anything"',
            f'[e8] button "{"x" * 90}"',
            '[e9] input[tel] "Telephone:"',
            '[e10] input[text] " : "',
            "not an element",
        ]
    }
    found = tabs.labels(view)
    assert found == {
        "e1": "Sign in",
        "e2": "Home",
        "e3": "Customer name",
        "e4": 'Say "yes"',
        "e6": "Size",
        "e7": "Notes",
        "e8": "x" * (tabs.LABEL_CHARS - 1) + "…",
        "e9": "Telephone",
    }
    assert tabs.describe("fill", "Customer name", "Ada") == "Filled in Customer name"


def test_a_card_without_a_title_or_with_a_bot_checks_names_the_site_not_the_address(
    tmp_path,
):
    @run
    async def test(r, podman, clock):
        await r.op_open(scope(), "https://gitlab.com/users/sign_in")
        tab = r.threads[("career", "7")]
        tab.title = "Just a moment..."
        assert r.card(tab).startswith("[![Browser: gitlab.com](")
        tab.title, tab.url = "", "https://email.news.example.co.uk/unsubscribe?t=s3cret"
        assert r.card(tab).startswith("[![Browser: example.co.uk](")
        tab.url = "http://10.1.2.3:8080/a?t=s3cret"
        assert r.card(tab).startswith("[![Browser: 10.1.2.3](")
        tab.url = ""
        assert r.card(tab).startswith("[![Browser: a page](")
        tab.title = "Sign in · GitLab"
        assert r.card(tab).startswith("[![Browser: Sign in · GitLab](")

    test(tmp_path)


def test_without_a_public_host_there_is_no_card(tmp_path):
    @run
    async def test(r, podman, clock):
        assert (await r.op_open(scope(), "example.com"))["card"] == ""

    test(tmp_path, pages_url="")


def test_act_and_read_need_an_open_page(tmp_path):
    @run
    async def test(r, podman, clock):
        with pytest.raises(RunnerError, match="no page open"):
            await r.op_act(scope(), "click", "e1")
        with pytest.raises(RunnerError, match="no page open"):
            await r.op_read(scope())
        assert podman.runs() == []

    test(tmp_path)


def test_a_read_with_card_gives_the_chats_card_whenever_it_has_a_tab(tmp_path):
    @run
    async def test(r, podman, clock):
        with pytest.raises(RunnerError, match="hasn't used the browser yet"):
            await r.op_read(scope(), card=True)
        opened = await r.op_open(scope(), "example.com")
        shown = await r.op_read(scope(), card=True)
        assert shown["card"] == opened["card"] and "Page: " in shown["page"]
        assert "card" not in await r.op_read(scope())  # only when asked
        # The user's browser: the card still, and why the page isn't read.
        await r.take(r.sessions["career"])
        held = await r.op_read(scope(), card=True)
        assert held["card"] == opened["card"]
        assert held["page"].startswith("The page can't be read now: the user has")
        await r.give_back(r.sessions["career"])
        # A stopped browser: the tab's card, which shows its last look.
        await r.stop("career")
        stopped = await r.op_read(scope(), card=True)
        assert stopped["card"] == opened["card"] and "no page open" in stopped["page"]
        assert ("career", "8") not in r.threads  # another chat's has none

    test(tmp_path)


def test_workspaces_get_browsers_of_their_own_and_the_idlest_gives_way(tmp_path):
    @run
    async def test(r, podman, clock):
        await r.op_open(scope("career"), "a.example")
        clock.t += 10
        await r.op_open(scope("education"), "b.example")
        assert {s.ip for s in r.sessions.values()} == {"10.89.79.32", "10.89.79.33"}
        clock.t += 10
        await r.op_open(scope("education"), "b2.example")  # education was used last
        await r.op_open(scope("hobby"), "c.example")
        assert set(r.sessions) == {"education", "hobby"}
        assert ["stop", "-t", "5", "everythingllm-browser-career"] in podman.calls
        assert not r.threads[("career", "7")].open
        # A browser someone watches isn't stopped for another.
        for s in r.sessions.values():
            s.viewers = 1
        with pytest.raises(RunnerError, match="all 2 browsers are in use"):
            await r.op_open(scope("career"), "a.example")

    test(tmp_path)


def test_two_workspaces_starting_at_once_get_slots_of_their_own(tmp_path):
    @run
    async def test(r, podman, clock):
        await asyncio.gather(
            r.op_open(scope("career"), "a.example"),
            r.op_open(scope("education"), "b.example"),
        )
        assert len({s.slot for s in r.sessions.values()}) == 2
        assert {s.ip for s in r.sessions.values()} == {"10.89.79.32", "10.89.79.33"}

    test(tmp_path)


def test_an_idle_browser_is_stopped_and_its_tab_comes_back_with_its_card(tmp_path):
    @run
    async def test(r, podman, clock):
        first = await r.op_open(scope(), "example.com")
        clock.t += runner_mod.IDLE - 1
        await r.stop_idle()
        assert "career" in r.sessions
        r.threads[("career", "7")].viewers = 1  # a card being watched keeps it up
        clock.t += runner_mod.IDLE * 2
        await r.stop_idle()
        assert "career" in r.sessions
        r.threads[("career", "7")].viewers = 0
        clock.t += runner_mod.IDLE + 1
        await r.stop_idle()
        assert "career" not in r.sessions
        tab = r.threads[("career", "7")]
        assert not tab.open and "browser closed" in tab.last
        with pytest.raises(RunnerError, match="no page open"):
            await r.op_read(scope())
        again = await r.op_open(scope(), "example.com")
        assert again["new"] and again["card"] == first["card"] and tab.open
        assert len(podman.runs()) == 2

    test(tmp_path)


def test_while_the_user_has_the_browser_the_agent_waits_for_them(tmp_path):
    @run
    async def test(r, podman, clock):
        await r.op_open(scope(), "login.example")
        handed = await r.op_handoff(scope(), "log in to the site")
        s = r.sessions["career"]
        tab = r.threads[("career", "7")]
        assert handed == {
            "card": r.card(tab),
            "takeover": f"https://host.example.ts.net:8454/{s.token}/?tab={tab.id}",
        }
        assert (
            s.control == "user"
            and s.asked
            and tab.last == "Waiting for you: log in to the site"
        )
        assert ("front", {"thread": "7"}) in podman.drivers[s.name].calls
        with pytest.raises(
            RunnerError,
            match=r"the user has this workspace's browser \(log in to the site\)\. When they say they're done",
        ):
            await r.op_act(scope(), "click", "e1")
        with pytest.raises(RunnerError, match="the user has"):
            await r.op_open(scope(), "other.example")
        # nor read, which would show what the user types (a password they reveal)
        with pytest.raises(RunnerError, match="the user has"):
            await r.op_read(scope())
        with pytest.raises(RunnerError, match="the user has"):
            await r.op_close(scope())
        back = await r.op_handoff(scope(), done=True)  # they said so in the chat
        assert "Sign in" in back["page"] and s.control == "agent" and not s.asked
        assert tab.last == "The agent has the browser again"
        assert (await r.op_act(scope(), "click", "e1"))["page"]

    test(tmp_path)


def test_taking_back_needs_a_browser(tmp_path):
    @run
    async def test(r, podman, clock):
        with pytest.raises(RunnerError, match="isn't open"):
            await r.op_handoff(scope(), done=True)

    test(tmp_path)


def test_a_stopped_browser_comes_back_in_the_agents_hands(tmp_path):
    @run
    async def test(r, podman, clock):
        await r.op_open(scope(), "login.example")
        await r.op_handoff(scope(), "log in")
        await r.stop("career")
        await r.op_open(scope(), "login.example")
        assert r.sessions["career"].control == "agent"

    test(tmp_path)


def test_taking_over_from_the_view_stops_the_agent_until_handed_back(tmp_path):
    @run
    async def test(r, podman, clock):
        await r.op_open(scope(), "example.com")
        s = r.sessions["career"]
        await r.take(s)
        assert s.control == "user" and s.reason == "you took over"
        with pytest.raises(RunnerError, match="you took over"):
            await r.op_act(scope(), "click", "e1")
        assert r.threads[("career", "7")].last == "You took over the browser"
        await r.give_back(s)
        await r.op_act(scope(), "click", "e1")

    test(tmp_path)


def test_a_browser_that_went_away_is_started_again_by_open(tmp_path):
    @run
    async def test(r, podman, clock):
        await r.op_open(scope(), "example.com")
        await podman.kill("everythingllm-browser-career")  # its window was closed
        with pytest.raises(RunnerError, match="the browser closed"):
            await r.op_act(scope(), "click", "e1")
        assert "career" not in r.sessions
        await podman.kill("everythingllm-browser-career")
        assert (await r.op_open(scope(), "example.com"))["new"]
        assert len(podman.runs()) == 2

    test(tmp_path)


def test_closing_a_tab_closes_it_in_the_browser(tmp_path):
    @run
    async def test(r, podman, clock):
        await r.op_open(scope(), "example.com")
        assert await r.op_close(scope()) == {}
        s = r.sessions["career"]
        assert ("close", {"thread": "7"}) in podman.drivers[s.name].calls
        assert not r.threads[("career", "7")].open
        assert await r.op_close(scope()) == {}  # again: nothing to do

    test(tmp_path)


def test_a_screenshot_is_taken_at_most_every_so_often(tmp_path):
    @run
    async def test(r, podman, clock):
        await r.op_open(scope(), "example.com")
        tab = r.threads[("career", "7")]
        shot = await r.screenshot(tab, 1.0)
        assert shot[:2] == b"\xff\xd8" and tab.title == "Shot"
        tab.shot = b"old"
        assert await r.screenshot(tab, 1.0) == b"old"  # the clock hasn't moved
        clock.t += 1
        assert (await r.screenshot(tab, 1.0))[:2] == b"\xff\xd8"

    test(tmp_path)


def test_a_tab_says_whether_the_agent_is_at_work_in_it_or_waits_for_the_user(tmp_path):
    @run
    async def test(r, podman, clock):
        async def op(name, **args):
            reply = await r.reply({"op": name, "args": {"scope": scope(), **args}})
            assert reply["ok"], reply
            return reply["result"]

        await op("open", url="https://linkedin.com/")
        tab = r.threads[("career", "7")]
        s = r.sessions["career"]
        assert r.state(tab) == r.activity(s) == "working"
        clock.t += runner_mod.ACTIVE  # the agent's answer has ended
        assert r.state(tab) == r.activity(s) == "idle"

        await r.reply({"op": "read", "args": {"scope": scope(thread="8")}})  # refused
        assert set(r.acted) == {("career", "8")}  # a chat's last op is let go once past

        tab.changed.clear()
        reading = asyncio.ensure_future(op("read"))
        await asyncio.sleep(0)
        assert r.state(tab) == "working" and tab.changed.is_set()  # while it runs
        await reading
        clock.t += runner_mod.ACTIVE

        saved = r.vault.add("career", "linkedin.com", "alice", "pw", ask=True)
        approval = (await op("login", login=saved["id"]))["approval"]
        assert r.state(tab) == "waiting"  # for the user's OK
        r.answer(s, approval, False)
        assert r.state(tab) == "working"
        clock.t += runner_mod.ACTIVE
        await op("ask_login")
        assert r.state(tab) == r.activity(s) == "waiting"  # for a login
        r.decline(next(iter(r.asked.values())))
        assert r.state(tab) == "working"

        await op("handoff", reason="log in")
        assert r.state(tab) == r.activity(s) == "waiting"
        await op("handoff", done=True)
        await r.take(s)
        assert r.state(tab) == r.activity(s) == "user"
        await r.give_back(s)
        await r.stop("career")
        assert r.state(tab) == "closed"

    test(tmp_path)


def test_the_starting_runner_removes_its_old_containers(tmp_path):
    @run
    async def test(r, podman, clock):
        await r.cleanup()
        assert podman.calls == [["rm", "-f", "--filter", f"label={containers.LABEL}"]]

    test(tmp_path)


RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def test_the_agent_logs_in_with_a_saved_login_without_seeing_it(tmp_path):
    @run
    async def test(r, podman, clock):
        saved = r.vault.add(
            "career", "linkedin.com", "alice", "hunter2", totp=RFC_SECRET
        )
        r.vault.add("education", "linkedin.com", "bob", "other")
        await r.op_open(scope(), "https://www.linkedin.com/login")
        listed = await r.op_logins(scope())
        assert listed == {
            "site": "www.linkedin.com",
            "logins": [{**r.vault.logins("career")[0], "here": True}],
        }
        assert "hunter2" not in str(listed) and RFC_SECRET not in str(listed)
        done = await r.op_login(scope(), saved["id"], "e1", "e2", True)
        assert "hunter2" not in str(done) and "Sign in" in done["page"]
        driver = podman.drivers["everythingllm-browser-career"]
        assert driver.filled[-1] == {"thread": "7", "site": "linkedin.com", "username": "alice",
                                     "password": "hunter2", "user_ref": "e1", "pass_ref": "e2", "submit": True}  # fmt: skip
        assert r.threads[("career", "7")].last == "Logged in for linkedin.com as alice"
        assert r.vault.logins("career")[0]["used"]
        coded = await r.op_code(scope(), saved["id"], "e3")
        assert "Sign in" in coded["page"] and len(driver.filled[-1]["code"]) == 6
        # Another workspace's login isn't there, and a login fills only on its site.
        await r.op_open(scope("education"), "https://www.linkedin.com/login")
        with pytest.raises(RunnerError, match="no saved login"):
            await r.op_login(scope("education"), saved["id"], "e1", "e2")
        await r.op_open(scope(), "https://evil.example/login")
        with pytest.raises(
            RunnerError,
            match="that login is for linkedin.com, and this chat's page is on evil.example",
        ):
            await r.op_login(scope(), saved["id"], "e1", "e2")

    test(tmp_path)


def test_a_login_without_2fa_has_no_code(tmp_path):
    @run
    async def test(r, podman, clock):
        saved = r.vault.add("career", "linkedin.com", "alice", "pw")
        await r.op_open(scope(), "https://linkedin.com/")
        with pytest.raises(RunnerError, match="no 2FA secret"):
            await r.op_code(scope(), saved["id"], "e3")

    test(tmp_path)


def test_a_login_that_asks_waits_for_the_users_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "WAIT", 0.05)

    @run
    async def test(r, podman, clock):
        saved = r.vault.add("career", "linkedin.com", "alice", "pw", ask=True)
        await r.op_open(scope(), "https://linkedin.com/")
        driver = podman.drivers["everythingllm-browser-career"]
        waiting = await r.op_login(scope(), saved["id"], "e1", "e2")
        assert waiting["card"] and driver.filled == []
        s = r.sessions["career"]
        assert list(s.approvals) == [waiting["approval"]]
        assert (
            r.threads[("career", "7")].last
            == "Waiting for your OK to use your linkedin.com login"
        )
        assert await r.op_wait_approval(scope(), waiting["approval"]) == {
            "done": False,
            "approved": False,
        }
        again = await r.op_login(scope(), saved["id"], "e1", "e2")
        assert (
            again["approval"] == waiting["approval"]
        )  # the same request, not a new one
        r.answer(s, waiting["approval"], False)
        assert await r.op_wait_approval(scope(), waiting["approval"]) == {
            "done": True,
            "approved": False,
        }
        asked = await r.op_login(scope(), saved["id"], "e1", "e2")
        r.answer(s, asked["approval"], True)
        assert (await r.op_login(scope(), saved["id"], "e1", "e2"))["page"]
        assert driver.filled[-1]["password"] == "pw"
        clock.t += runner_mod.GRANT + 1  # the OK runs out
        assert "approval" in await r.op_login(scope(), saved["id"], "e1", "e2")
        with pytest.raises(RunnerError, match="isn't waiting"):
            r.answer(s, "nope", True)

    test(tmp_path)


def test_a_request_no_longer_asked_is_stale_not_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "WAIT", 5)
    stale = {"done": True, "approved": False, "stale": True}

    @run
    async def test(r, podman, clock):
        alice = r.vault.add("career", "linkedin.com", "alice", "pw", ask=True)
        bob = r.vault.add("career", "linkedin.com", "bob", "pw", ask=True)
        await r.op_open(scope(), "https://linkedin.com/")
        first = (await r.op_login(scope(), alice["id"], "e1", "e2"))["approval"]
        waiting = asyncio.create_task(r.op_wait_approval(scope(), first))
        await asyncio.sleep(0)
        # The chat's request for another login takes its place: the waiter hears at once.
        second = (await r.op_login(scope(), bob["id"], "e1", "e2"))["approval"]
        assert second != first
        assert await asyncio.wait_for(waiting, 1) == stale
        assert await r.op_wait_approval(scope(), first) == stale
        assert await r.op_wait_approval(scope(), "nope") == stale
        assert list(r.sessions["career"].approvals) == [second]
        # So does one whose browser stops, and it stays stale.
        waiting = asyncio.create_task(r.op_wait_approval(scope(), second))
        await asyncio.sleep(0)
        await r.stop("career")
        assert await asyncio.wait_for(waiting, 1) == stale
        assert await r.op_wait_approval(scope(), second) == stale
        # And one whose browser went away under a call (its window closed, a crash).
        await r.op_open(scope(), "https://linkedin.com/")
        third = (await r.op_login(scope(), alice["id"], "e1", "e2"))["approval"]
        waiting = asyncio.create_task(r.op_wait_approval(scope(), third))
        await asyncio.sleep(0)
        await podman.kill("everythingllm-browser-career")
        with pytest.raises(RunnerError, match="the browser closed"):
            await r.op_read(scope())
        assert await asyncio.wait_for(waiting, 1) == stale

    test(tmp_path)


def test_two_chats_wait_for_their_ok_at_once_and_each_answer_is_its_own(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(runner_mod, "WAIT", 5)

    @run
    async def test(r, podman, clock):
        alice = r.vault.add("career", "linkedin.com", "alice", "pw", ask=True)
        bob = r.vault.add("career", "linkedin.com", "bob", "pw", ask=True)
        await r.op_open(scope(thread="7"), "https://linkedin.com/")
        await r.op_open(scope(thread="8"), "https://linkedin.com/feed")
        s = r.sessions["career"]
        seven, eight = r.threads[("career", "7")], r.threads[("career", "8")]
        first = (await r.op_login(scope(thread="7"), alice["id"], "e1", "e2"))[
            "approval"
        ]
        second = (await r.op_login(scope(thread="8"), bob["id"], "e1", "e2"))[
            "approval"
        ]
        assert list(s.approvals) == [first, second]  # in the order asked
        waits = [
            asyncio.create_task(r.op_wait_approval(scope(thread="7"), first)),
            asyncio.create_task(r.op_wait_approval(scope(thread="8"), second)),
        ]
        await asyncio.sleep(0)
        assert not any(w.done() for w in waits)  # neither made the other's stale
        assert r.state(seven) == r.state(eight) == "waiting"

        r.answer(s, second, True)
        assert await asyncio.wait_for(waits[1], 1) == {"done": True, "approved": True}
        await asyncio.sleep(0)
        assert not waits[0].done()
        assert eight.last == "You allowed the linkedin.com login"
        assert seven.last == "Waiting for your OK to use your linkedin.com login"
        assert r.state(seven) == "waiting"

        r.answer(s, first, False)
        assert await asyncio.wait_for(waits[0], 1) == {"done": True, "approved": False}
        assert seven.last == "You refused the linkedin.com login"
        assert eight.last == "You allowed the linkedin.com login"
        assert s.approvals == {}

    test(tmp_path)


def test_a_request_nobody_answers_is_let_go(tmp_path):
    stale = {"done": True, "approved": False, "stale": True}

    @run
    async def test(r, podman, clock):
        alice = r.vault.add("career", "linkedin.com", "alice", "pw", ask=True)
        await r.op_open(scope(thread="7"), "https://linkedin.com/")
        await r.op_open(scope(thread="8"), "https://linkedin.com/feed")
        s = r.sessions["career"]
        old = (await r.op_login(scope(thread="7"), alice["id"], "e1", "e2"))["approval"]
        r.answer(s, old, True)
        old = (await r.op_login(scope(thread="8"), alice["id"], "e1", "e2"))["approval"]
        clock.t += runner_mod.APPROVAL_SECONDS + 1
        # Another chat's request lets it go, and the OKs past their time with it.
        new = (await r.op_login(scope(thread="7"), alice["id"], "e1", "e2"))["approval"]
        assert list(s.approvals) == [new] and s.granted == {}
        assert await r.op_wait_approval(scope(thread="8"), old) == stale
        tab = r.threads[("career", "8")]
        assert tab.last == "Nobody answered in time about the linkedin.com login"
        assert r.state(tab) == "idle"
        with pytest.raises(RunnerError, match="isn't waiting"):
            r.answer(s, old, True)

    test(tmp_path)


def test_a_fill_done_isnt_undone_by_noting_its_use(tmp_path):
    @run
    async def test(r, podman, clock):
        saved = r.vault.add("career", "linkedin.com", "alice", "pw")
        await r.op_open(scope(), "https://linkedin.com/")

        def gone(*args, **kw):
            raise VaultError("there's no saved login")

        r.vault.update = gone  # deleted in the take-over view while it was filled
        assert (await r.op_login(scope(), saved["id"], "e1", "e2"))["page"]

    test(tmp_path)


def test_an_offer_is_kept_when_its_save_would_be_refused(tmp_path):
    @run
    async def test(r, podman, clock):
        await r.op_open(scope(), "https://linkedin.com/")
        s = r.sessions["career"]
        driver = podman.drivers[s.name]
        await r.op_handoff(scope(), "log in")
        driver.offers["ab12cd34"] = {"site": "linkedin.com", "username": "alice", "password": "typed"}  # fmt: skip
        with pytest.raises(RunnerError, match="too long"):
            await r.save_offer(s, "ab12cd34", "a" * 1001, False)
        assert "ab12cd34" in driver.offers and r.vault.logins("career") == []

    test(tmp_path)


def test_logins_are_offered_for_saving_only_while_the_user_has_the_browser(tmp_path):
    @run
    async def test(r, podman, clock):
        await r.op_open(scope(), "https://linkedin.com/")
        s = r.sessions["career"]
        driver = podman.drivers[s.name]
        await r.op_handoff(scope(), "log in")
        assert driver.capturing
        driver.offers["ab12cd34"] = {
            "site": "linkedin.com",
            "username": "alice",
            "password": "typed",
        }
        assert await r.offers(s) == [
            {"id": "ab12cd34", "site": "linkedin.com", "username": "alice"}
        ]
        saved = await r.save_offer(s, "ab12cd34", "alice@example.com", True)
        assert saved["username"] == "alice@example.com" and saved["ask"]
        assert r.vault.get("career", saved["id"])["password"] == "typed"
        await r.op_handoff(scope(), done=True)
        assert not driver.capturing
        await r.take(s)
        assert driver.capturing

    test(tmp_path)


def test_the_agent_asks_for_a_login_for_its_pages_site_and_the_user_saves_it(tmp_path):
    @run
    async def test(r, podman, clock):
        with pytest.raises(RunnerError, match="no page open"):
            await r.op_ask_login(scope())
        await r.op_open(scope(), "https://accounts.google.com/signin?x=1")
        asked = await r.op_ask_login(scope())
        assert asked["site"] == "accounts.google.com"
        assert asked["card"] == (
            "[![Log in to google.com](https://host.example.ts.net:8445/_live/browser/login/"
            f"{asked['request']}.png)](https://host.example.ts.net:8445/_live/browser/login/{asked['request']})"
        )
        req = r.asked[asked["request"]]
        assert len(req.id) == 35 and req.sites == ["accounts.google.com", "google.com"]
        assert req.url == "https://accounts.google.com/signin?x=1"
        tab = r.threads[("career", "7")]
        assert tab.last == "Waiting for your login for accounts.google.com"
        # Asking again while it waits is the same request; another thread's is its own.
        assert (await r.op_ask_login(scope()))["request"] == req.id
        await r.op_open(scope(thread="8"), "https://accounts.google.com/")
        assert (await r.op_ask_login(scope(thread="8")))["request"] != req.id
        # Only for one of the request's sites, and with a password.
        with pytest.raises(RunnerError, match="not 'evil.example'"):
            await r.fulfil(req, "evil.example", "alice", "pw", "", False)
        with pytest.raises(RunnerError, match="not 'com'"):
            await r.fulfil(req, "com", "alice", "pw", "", False)
        with pytest.raises(RunnerError, match="enter the password"):
            await r.fulfil(req, "google.com", "alice", "", "", False)
        saved = await r.fulfil(req, "google.com", "alice", "hunter2", "", True)
        assert "hunter2" not in str(saved)
        assert saved["site"] == "google.com" and saved["ask"]
        assert r.vault.get("career", saved["id"])["password"] == "hunter2"
        assert r.asked_state(req) == "saved" and req.changed.is_set()
        assert tab.last == "You saved a login for google.com"
        # Once.
        with pytest.raises(RunnerError, match=r"isn't waiting any more \(saved\)"):
            await r.fulfil(req, "google.com", "mallory", "x", "", False)
        with pytest.raises(RunnerError, match="isn't waiting"):
            r.decline(req)
        assert len(r.vault.logins("career")) == 1
        # The agent fills it like any other.
        listed = await r.op_logins(scope())
        assert listed["logins"][0]["here"]

    test(tmp_path)


@pytest.mark.parametrize(
    "url", ["http://10.0.0.1/login", "https://github.io/", "about:blank"]
)
def test_a_page_without_a_site_of_its_own_cant_be_asked_for(tmp_path, url):
    @run
    async def test(r, podman, clock):
        await r.op_open(scope(), url)
        with pytest.raises(RunnerError, match="can't have a saved login"):
            await r.op_ask_login(scope())
        assert r.asked == {}

    test(tmp_path)


def test_a_request_runs_out_is_kept_a_while_and_is_capped(tmp_path):
    @run
    async def test(r, podman, clock):
        await r.op_open(scope(), "https://linkedin.com/")
        first = r.asked[(await r.op_ask_login(scope()))["request"]]
        assert first.sites == ["linkedin.com"]
        clock.t += logins.ASK_SECONDS + 1
        assert r.asked_state(first) == "expired"
        with pytest.raises(RunnerError, match=r"\(expired\)"):
            await r.fulfil(first, "linkedin.com", "alice", "pw", "", False)
        # A new one takes its place, and the old one's card still says how it ended,
        # for a day.
        second = (await r.op_ask_login(scope()))["request"]
        assert second != first.id and r.asked_by_id(first.id) is first
        clock.t += logins.KEEP_ASKED - logins.ASK_SECONDS
        assert r.asked_by_id(first.id) is None and r.asked_by_id("lr-nope") is None
        second = (await r.op_ask_login(scope()))["request"]
        # The browser stopping doesn't end one: the save needs only the vault.
        await r.stop("career")
        req = r.asked_by_id(second)
        await r.fulfil(req, "linkedin.com", "alice", "pw", "", False)
        assert r.vault.logins("career")[0]["site"] == "linkedin.com"
        # At most MAX_ASKED wait in a workspace.
        for n in range(logins.MAX_ASKED):
            await r.op_open(scope(thread=f"t{n}"), "https://linkedin.com/")
            await r.op_ask_login(scope(thread=f"t{n}"))
        await r.op_open(scope(thread="last"), "https://linkedin.com/")
        with pytest.raises(RunnerError, match="already waiting"):
            await r.op_ask_login(scope(thread="last"))
        await r.op_open(scope("education"), "https://linkedin.com/")
        assert (await r.op_ask_login(scope("education")))["site"] == "linkedin.com"

    test(tmp_path)


def test_a_save_the_vault_refuses_leaves_the_request_waiting(tmp_path):
    @run
    async def test(r, podman, clock):
        await r.op_open(scope(), "https://linkedin.com/")
        req = r.asked[(await r.op_ask_login(scope()))["request"]]
        with pytest.raises(RunnerError, match="too long"):
            await r.fulfil(req, "linkedin.com", "a" * 1001, "pw", "", False)
        assert r.asked_state(req) == "waiting"
        r.decline(req)
        assert r.asked_state(req) == "declined"
        assert (
            r.threads[("career", "7")].last
            == "You didn't give a login for linkedin.com"
        )

    test(tmp_path)


def test_the_agent_cant_ask_while_the_user_has_the_browser(tmp_path):
    @run
    async def test(r, podman, clock):
        await r.op_open(scope(), "https://linkedin.com/")
        await r.take(r.sessions["career"])
        with pytest.raises(RunnerError, match="the user has"):
            await r.op_ask_login(scope())

    test(tmp_path)


def test_a_download_is_copied_to_project_downloads_and_said_in_the_threads_read(
    tmp_path,
):
    @run
    async def test(r, podman, clock):
        await r.op_open(scope(thread="7"), "example.com")
        staged = tmp_path / "data" / "downloads" / "career" / "7"
        staged.mkdir()
        (staged / "a.pdf").write_bytes(b"%PDF")
        (staged / ".b.pdf.part").write_bytes(b"half")  # still being saved
        downloads = tmp_path / "workspaces" / "career" / "project" / "downloads"
        downloads.mkdir(parents=True)
        (downloads / "a.pdf").write_bytes(b"older")
        read = await r.op_read(scope(thread="7"))
        assert "downloaded a-2.pdf to /project/downloads/a-2.pdf" in read["page"]
        assert (downloads / "a-2.pdf").read_bytes() == b"%PDF"
        assert (downloads / "a.pdf").read_bytes() == b"older"
        assert not (staged / "a.pdf").exists() and (staged / ".b.pdf.part").exists()
        assert "downloaded" not in (await r.op_read(scope(thread="7")))["page"]

    test(tmp_path)


def test_a_download_for_another_thread_waits_for_that_threads_read(tmp_path):
    @run
    async def test(r, podman, clock):
        await r.op_open(scope(thread="7"), "example.com")
        await r.op_open(scope(thread="8"), "example.org")
        staged = tmp_path / "data" / "downloads" / "career" / "8"
        staged.mkdir()
        (staged / "x.csv").write_text("1,2")
        assert "x.csv" not in (await r.op_read(scope(thread="7")))["page"]
        assert "downloaded x.csv" in (await r.op_read(scope(thread="8")))["page"]

    test(tmp_path)


def test_a_run_cant_send_downloads_through_a_symlink(tmp_path):
    """A sandbox run can make /project/downloads a symlink to anywhere on the host: the
    browser never mounts it, and the copy refuses to follow it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    staging = tmp_path / "data" / "downloads" / "career"
    project = tmp_path / "workspaces" / "career" / "project"
    project.mkdir(parents=True)
    (project / "downloads").symlink_to(outside)
    (staging / "7").mkdir(parents=True)
    (staging / "7" / "evil.pth").write_text("import os")
    told = containers.collect_downloads(staging, tmp_path / "workspaces", "career")
    assert list(outside.iterdir()) == []
    assert told == {
        "7": [
            "couldn't save the download evil.pth to /project/downloads: it isn't a plain folder"
        ]
    }
    assert not (staging / "7" / "evil.pth").exists()
    # nor through a symlinked /project/downloads/<name>
    (project / "downloads").unlink()
    (project / "downloads").mkdir()
    (project / "downloads" / "evil.pth").symlink_to(outside / "planted")
    (staging / "7" / "evil.pth").write_text("import os")
    told = containers.collect_downloads(staging, tmp_path / "workspaces", "career")
    assert told == {"7": ["downloaded evil-2.pth to /project/downloads/evil-2.pth"]}
    assert list(outside.iterdir()) == []


def test_the_browser_cant_make_the_copy_read_host_files(tmp_path):
    """The container can write its /downloads: a symlink there, as a file or a thread's
    folder, isn't followed."""
    secret = tmp_path / "secret.txt"
    secret.write_text("vault key")
    staging = tmp_path / "data" / "downloads" / "career"
    (staging / "7").mkdir(parents=True)
    (staging / "7" / "key.txt").symlink_to(secret)
    (staging / "8").symlink_to(tmp_path)
    told = containers.collect_downloads(staging, tmp_path / "workspaces", "career")
    downloads = tmp_path / "workspaces" / "career" / "project" / "downloads"
    assert not downloads.exists() or list(downloads.iterdir()) == []
    assert "8" not in told and told.get("7", []) == []
    assert secret.read_text() == "vault key"
    assert (tmp_path / "data").is_dir()  # nothing behind the symlinked folder removed


def test_an_oversized_download_isnt_copied(tmp_path, monkeypatch):
    monkeypatch.setattr(containers, "DOWNLOAD_BYTES", 3)
    staging = tmp_path / "data" / "downloads" / "career"
    (staging / "7").mkdir(parents=True)
    (staging / "7" / "big.bin").write_bytes(b"1234")
    told = containers.collect_downloads(staging, tmp_path / "workspaces", "career")
    assert told["7"][0].startswith("couldn't save the download big.bin")
    assert not (staging / "7" / "big.bin").exists()


def test_only_the_user_taking_over_in_the_view_unlocks_the_browser(tmp_path):
    @run
    async def test(r, podman, clock):
        await r.op_open(scope(), "https://linkedin.com/")
        s = r.sessions["career"]
        driver = podman.drivers[s.name]
        await r.op_handoff(scope(), "log in")
        assert (
            driver.capturing and not driver.taken
        )  # the agent's handoff unlocks nothing
        await r.op_handoff(scope(), done=True)
        await r.take(s)
        assert driver.capturing and driver.taken

    test(tmp_path)


def test_an_ok_to_use_a_login_is_for_the_chat_that_asked_alone(tmp_path):
    @run
    async def test(r, podman, clock):
        saved = r.vault.add("career", "linkedin.com", "alice", "pw", ask=True)
        await r.op_open(scope(thread="7"), "https://linkedin.com/")
        await r.op_open(scope(thread="8"), "https://linkedin.com/feed")
        s = r.sessions["career"]
        asked = await r.op_login(scope(thread="7"), saved["id"], "e1", "e2")
        waiting = s.approvals[asked["approval"]]
        assert (waiting.thread, waiting.url) == ("7", "https://linkedin.com/")
        r.answer(s, asked["approval"], True)
        assert (await r.op_login(scope(thread="7"), saved["id"], "e1", "e2"))["page"]
        other = await r.op_login(scope(thread="8"), saved["id"], "e1", "e2")
        assert "approval" in other
        assert [a.thread for a in s.approvals.values()] == ["8"]

    test(tmp_path)


def test_a_login_fills_only_on_an_https_page_on_its_usual_port(tmp_path):
    @run
    async def test(r, podman, clock):
        saved = r.vault.add("career", "linkedin.com", "alice", "pw")
        for url in ("http://linkedin.com/login", "https://linkedin.com:8443/login"):
            await r.op_open(scope(), url)
            with pytest.raises(RunnerError, match="only on an https page"):
                await r.op_login(scope(), saved["id"], "e1", "e2")
        assert podman.drivers["everythingllm-browser-career"].filled == []

    test(tmp_path)


def test_the_agent_signs_in_with_a_passkey_without_seeing_it(tmp_path):
    @run
    async def test(r, podman, clock):
        saved = r.vault.add_passkey("career", passkey("github.com"))
        r.vault.update("career", saved["id"], ask=False)
        login = r.vault.add("career", "github.com", "alice", "pw")
        await r.op_open(scope(), "https://github.com/login")
        driver = podman.drivers["everythingllm-browser-career"]
        listed = await r.op_logins(scope())
        assert listed["logins"][0] == {**r.vault.logins("career")[0], "here": True}
        assert passkey()["privateKey"] not in str(listed)
        done = await r.op_passkey(scope(), saved["id"], "e1")
        assert passkey()["privateKey"] not in str(done) and "Sign in" in done["page"]
        assert driver.filled[-1] == {
            "thread": "7", "site": "github.com", "ref": "e1",
            "credential": as_given(passkey()),
        }  # fmt: skip
        assert (
            r.threads[("career", "7")].last
            == "Signed in with a passkey for github.com as alice"
        )
        entry = r.vault.get("career", saved["id"])
        assert entry["sign_count"] == 2 and entry["used"]
        # A page that never asks: said so, and nothing counted.
        driver.asked_for_passkey = False
        await r.op_passkey(scope(), saved["id"], "e1")
        assert (
            r.threads[("career", "7")].last
            == "The page didn't ask for the github.com passkey"
        )
        assert r.vault.get("career", saved["id"])["sign_count"] == 2
        # Each is used only as what it is.
        with pytest.raises(RunnerError, match="is a login, not a passkey"):
            await r.op_passkey(scope(), login["id"], "e1")
        with pytest.raises(RunnerError, match="is a passkey, not a login"):
            await r.op_login(scope(), saved["id"], "e1", "e2")
        # Only on its own site, over https.
        calls = len(driver.filled)
        for url in ("https://evil.example/", "http://github.com/login"):
            await r.op_open(scope(), url)
            with pytest.raises(RunnerError, match="is for github.com|only on an https"):
                await r.op_passkey(scope(), saved["id"], "e1")
        assert len(driver.filled) == calls

    test(tmp_path)


def test_a_passkey_asks_first_unless_the_user_turned_it_off(tmp_path):
    @run
    async def test(r, podman, clock):
        saved = r.vault.add_passkey("career", passkey())
        await r.op_open(scope(), "https://github.com/login")
        driver = podman.drivers["everythingllm-browser-career"]
        waiting = await r.op_passkey(scope(), saved["id"], "e1")
        assert waiting["approval"] and driver.filled == []
        r.answer(r.sessions["career"], waiting["approval"], True)
        assert (await r.op_passkey(scope(), saved["id"], "e1"))["page"]
        assert driver.filled[-1]["ref"] == "e1"

    test(tmp_path)


def test_only_the_user_makes_passkeys_and_each_made_is_saved(tmp_path):
    @run
    async def test(r, podman, clock):
        await r.op_open(scope(), "https://github.com/settings/security")
        s = r.sessions["career"]
        driver = podman.drivers[s.name]
        with pytest.raises(RunnerError, match="take over the browser first"):
            await r.make_passkeys(s, True)
        assert not driver.making
        await r.take(s)
        await r.make_passkeys(s, True)
        assert driver.making and s.making
        # Saved when the view next asks, asking first; the driver stops after one.
        driver.made.append({"credential": passkey(), "url": "https://github.com/"})
        await r.save_made(s)
        assert not s.making and not driver.making
        [saved] = r.vault.logins("career")
        assert saved["kind"] == "passkey" and saved["site"] == "github.com"
        assert saved["ask"]
        assert (
            r.threads[("career", "7")].last
            == s.made
            == "Saved the passkey you made for github.com as alice"
        )
        # One made just before the hand-back is saved then; one the vault refuses is said.
        await r.make_passkeys(s, True)
        driver.made.append({"credential": passkey(credential_id="second"), "url": ""})
        driver.made.append({"credential": passkey("github.io"), "url": ""})
        await r.give_back(s)
        assert not s.making and not driver.making
        assert len(r.vault.logins("career")) == 2
        assert s.made.startswith("The passkey a site made couldn't be saved")
        # Taking over again starts afresh, and what's made is saved as the browser stops.
        await r.take(s)
        assert s.made == ""
        await r.make_passkeys(s, True)
        driver.made.append({"credential": passkey(credential_id="third"), "url": ""})
        await r.stop("career")
        assert len(r.vault.logins("career")) == 3

    test(tmp_path)


def test_the_card_names_a_pressed_key_only_when_its_a_named_one():
    describe = tabs.describe
    assert describe("press", "", "Enter") == "Pressed Enter"
    assert describe("press", "", "Shift+Tab") == "Pressed Shift+Tab"
    assert (
        describe("press", "", "h") == "Pressed a key"
    )  # a run of them could spell a password
    assert describe("press", "", "Shift+H") == "Pressed a key"
