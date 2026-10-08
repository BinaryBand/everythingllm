"""Model access for a run (sandbox.models), and the runner serving it to a run."""

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import hostrpc
import pytest
from sandbox import model_client, models, runner
from test_runner import A, FakePodman, cfg, go, mounts  # noqa: F401 - cfg is a fixture

NOW = datetime(2026, 10, 8, 23, 30, tzinfo=ZoneInfo("Europe/Stockholm"))


class Asked(list):
    """A model that answers in capitals and uses 10 tokens in and out."""

    def __call__(self, model, messages, max_tokens):
        self.append((model, messages, max_tokens))
        return messages[-1]["content"].upper(), {
            "prompt_tokens": 10,
            "completion_tokens": 10,
        }


def service(tmp_path, budget=50, asked=None, now=NOW):
    return models.Models(
        "career",
        "12",
        budget,
        tmp_path / "log",
        Asked() if asked is None else asked,
        now=lambda: now,
    )


def test_a_call_is_answered_logged_without_its_text_and_counted(tmp_path):
    asked = Asked()
    s = service(tmp_path, asked=asked)
    got = go(s.op_ask("say hi"))
    assert got == {
        "text": "SAY HI",
        "model": "deepseek-flash",
        "tokens": 20,
        "tokens_left": 30,
    }
    assert asked == [("deepseek-flash", [{"role": "user", "content": "say hi"}], 2048)]
    [line] = (tmp_path / "log" / "2026-10.jsonl").read_text().splitlines()
    entry = json.loads(line)
    assert entry == {
        "time": "2026-10-08T23:30:00+02:00",
        "day": "2026-10-08",
        "workspace": "career",
        "thread": "12",
        "model": "deepseek-flash",
        "tokens": 20,
    }
    assert "hi" not in line.lower().replace("thread", "")


def test_the_budget_is_the_workspaces_for_its_day(tmp_path):
    s = service(tmp_path)
    go(s.op_ask("a"))
    go(s.op_ask("b"))
    assert s.left() == 10
    go(s.op_ask("c"))  # under the budget when it started
    with pytest.raises(hostrpc.RunnerError, match="used its 50 model tokens for today"):
        go(s.op_ask("d"))
    # Another workspace, and the next day, have their own.
    other = models.Models("home", "1", 50, tmp_path / "log", Asked(), now=lambda: NOW)
    assert other.left() == 50
    tomorrow = service(tmp_path, now=NOW.replace(day=9))
    assert tomorrow.left() == 50


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"messages": "x", "model": "gpt-5"}, "model must be one of"),
        ({"messages": "x", "max_tokens": 9000}, "max_tokens must be"),
        ({"messages": "x", "max_tokens": True}, "max_tokens must be"),
        ({"messages": []}, "messages must be"),
        ({"messages": [{"role": "tool", "content": "x"}]}, "role one of"),
        ({"messages": [{"role": "user", "content": 5}]}, "each message"),
        ({"messages": "x" * (models.MAX_CHARS + 1)}, "over 200000 characters"),
    ],
)
def test_what_a_call_may_not_ask(tmp_path, args, error):
    asked = Asked()
    with pytest.raises(hostrpc.RunnerError, match=error):
        go(service(tmp_path, asked=asked).op_ask(**args))
    assert asked == []


def test_the_client_asks_through_the_socket(tmp_path, monkeypatch):
    sock = Path("/tmp") / f"models-test-{os.getpid()}.sock"
    s = service(tmp_path)

    async def main():
        async with hostrpc.serving(s, sock):
            monkeypatch.setattr(model_client, "MODELS", str(sock))
            answer = await asyncio.to_thread(
                model_client.ask, "hello", system="be loud"
            )
            with pytest.raises(RuntimeError, match="model must be one of"):
                await asyncio.to_thread(model_client.ask, "x", model="nope")
            return answer

    try:
        assert go(main()) == "HELLO"
    finally:
        sock.unlink(missing_ok=True)
    monkeypatch.setattr(model_client, "MODELS", "")
    with pytest.raises(RuntimeError, match="no model access"):
        model_client.ask("x")


def test_the_client_runs_with_the_standard_library_alone():
    done = subprocess.run(
        [sys.executable, "-I", "-S", model_client.__file__, "hi"],
        capture_output=True,
        text=True,
        env={},
        check=False,
    )
    assert done.returncode == 1 and "no model access" in done.stderr


def test_a_run_with_model_access_gets_its_own_socket_and_never_the_key(cfg):  # noqa: F811
    cfg.access_file.parent.mkdir(parents=True)
    cfg.access_file.write_text('{"career": {"models": true, "daily_tokens": 100}}')
    cfg.model_log = cfg.access_file.parent / "models"
    cfg.model_sockets = Path("/tmp") / f"models-test-{os.getpid()}"
    asked = Asked()
    seen = {}

    async def effect_async(m):
        # What the run's code does: ask through the socket it was given.
        sock = m["/run/everythingllm"] / "sock"
        seen["client"] = (m["/sandbox"] / "everythingllm_models.py").read_text()
        seen["reply"] = await hostrpc.request(
            sock, "ask", {"messages": "hi"}, 5, name="models"
        )

    class Podman(FakePodman):
        async def __call__(self, args, timeout, kill):
            if args[0] == "run":
                await effect_async(mounts(args))
            return await super().__call__(args, timeout, kill)

    r = runner.Runner(cfg, podman=Podman(), ask_model=asked)
    res = go(r.op_run(A, "python", "import everythingllm_models"))
    assert seen["reply"]["text"] == "HI" and asked
    assert res["models"] == {"tokens_left": 80}
    assert "def ask(" in seen["client"]
    args = r.podman.runs()[0][0]
    assert "EVERYTHINGLLM_MODELS=/run/everythingllm/sock" in args
    assert any(a.endswith(":/run/everythingllm:ro") for a in args)
    assert not any("KEY" in a or ".env" in a for a in args)
    assert not any(cfg.scripts.iterdir())
    assert not (cfg.model_sockets / args[args.index("--name") + 1]).exists()


def test_a_run_without_model_access_gets_no_socket(cfg):  # noqa: F811
    r = runner.Runner(cfg, podman=FakePodman(), ask_model=Asked())
    res = go(r.op_run(A, "python", "print(1)"))
    args = r.podman.runs()[0][0]
    assert res["models"] is None
    assert not any("EVERYTHINGLLM" in a or "/run/everythingllm" in a for a in args)


def test_turning_models_on_or_raising_the_budget_needs_approval(cfg):  # noqa: F811
    r = runner.Runner(cfg, podman=FakePodman(), ask_model=Asked())
    for change in ({"models": True}, {"daily_tokens": 300_000}):
        with pytest.raises(runner.SandboxError, match="needs the user's approval"):
            go(r.op_access(A, **change, apply=True))
    go(r.op_access(A, models=True, daily_tokens=300_000, apply=True, approved=True))
    lower = go(r.op_access(A, daily_tokens=1000, apply=True))  # lowering needs none
    assert (lower["models"], lower["daily_tokens"]) == (True, 1000)
    assert json.loads(cfg.access_file.read_text()) == {
        "career": {"web": False, "models": True, "daily_tokens": 1000}
    }
