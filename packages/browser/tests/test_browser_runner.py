"""browser-runner with podman faked (browser_fakes): a workspace's browser is started on its
first call, keeps to the workspace's own folders and an address of its own, gives each
thread a tab with a live card, refuses the agent while the user has the browser, and is
stopped when it's idle."""

import asyncio

import pytest
from browser import runner as runner_mod
from browser.page import UNTRUSTED
from browser.runner import Runner, check_scope
from browser_fakes import Clock, FakePodman, config, scope
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
    s = runner_mod.Session(
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
    home = tmp_path / "workspaces" / "career"
    data = "rw,noexec,nosuid,nodev"
    assert volumes == [
        "/repo:/repo:ro",
        f"{home}/browser/profile:/profile:{data}",
        f"{home}/project/downloads:/downloads:{data}",
        f"{tmp_path}/data/sockets/browser-1:/run/browser:{data}",
    ]
    assert args[-1] == runner_mod.IMAGE


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
        home = tmp_path / "workspaces" / "career"
        assert (home / "browser" / "profile").is_dir() and (
            home / "project" / "downloads"
        ).is_dir()
        assert not (home / "shared").exists()
        acted = await r.op_act(scope(thread="7"), "click", "e1")
        assert "Sign in" in acted["page"]
        found = await r.op_read(scope(thread="7"), "home")
        assert (
            '[e2] link "Home"' in found["page"]
            and "Sign in to go on" not in found["page"]
        )
        tab = r.threads[("career", "7")]
        assert tab.last == "Clicked e1" and tab.url == "example.org"

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
        assert (await r.op_read(scope()))["page"]  # reading is fine
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
        r.take(s)
        assert s.control == "user" and s.reason == "you took over"
        with pytest.raises(RunnerError, match="you took over"):
            await r.op_act(scope(), "click", "e1")
        assert r.threads[("career", "7")].last == "You took over the browser"
        r.give_back(s)
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
        assert r.state(tab) == "agent"
        r.sessions["career"].control = "user"
        assert r.state(tab) == "user"

    test(tmp_path)


def test_the_starting_runner_removes_its_old_containers(tmp_path):
    @run
    async def test(r, podman, clock):
        await r.cleanup()
        assert podman.calls == [["rm", "-f", "--filter", f"label={runner_mod.LABEL}"]]

    test(tmp_path)
