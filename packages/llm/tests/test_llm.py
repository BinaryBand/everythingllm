import json

import httpx
import pytest
from llm import (
    DEFAULT_MODEL,
    REPAIR,
    APIError,
    Completions,
    LLMError,
    Provider,
    chat_json,
    deepseek,
    parse_json,
    provider,
    provider_for,
    settings,
)


def test_parse_json_tolerates_fences_and_prose_but_wants_an_object():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here it is: {"a": [1]} done') == {"a": [1]}
    with pytest.raises(ValueError, match="wasn't a JSON object"):
        parse_json("[1, 2]")


def test_chat_json_repairs_once_then_gives_up():
    seen = []

    def chat(messages):
        seen.append(messages)
        return replies.pop(0)

    replies = ["not json", '{"a": 1}']
    assert chat_json(chat, [{"role": "user", "content": "q"}]) == {"a": 1}
    assert seen[1][1:] == [{"role": "assistant", "content": "not json"}, REPAIR]
    replies = ["nope", "[1]"]
    with pytest.raises(ValueError):
        chat_json(chat, [])
    assert len(seen) == 4


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANYTHINGLLM_ENV", raising=False)


def test_settings_read_anythingllms_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "OTHER=x\nDEEPSEEK_API_KEY='sk-file'\nDEEPSEEK_MODEL_PREF=\"deepseek-pro\"\n"
    )
    assert settings(str(env)) == ("sk-file", "deepseek-pro")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    assert settings(str(env)) == ("sk-env", "deepseek-pro")
    monkeypatch.setenv("ANYTHINGLLM_ENV", str(env))
    assert settings("/nowhere/.env") == ("sk-env", "deepseek-pro")


def test_settings_without_an_env_file(tmp_path):
    assert settings(str(tmp_path / "missing")) == ("", DEFAULT_MODEL)


def answering(status=200, finish="stop", content="hi", raw=None, **extra):
    """Requests sent and a transport answering them; a tuple of statuses is answered in
    turn, the last one from then on."""
    sent = []
    statuses = status if isinstance(status, tuple) else (status,)

    def handler(request):
        sent.append(request)
        status = statuses[min(len(sent), len(statuses)) - 1]
        if raw is not None:
            return httpx.Response(status, content=raw)
        choice = {"finish_reason": finish, "message": {"content": content}}
        return httpx.Response(status, json={"choices": [choice], **extra})

    return sent, httpx.MockTransport(handler)


def test_deepseek_asks_with_thinking_off():
    sent, transport = answering()
    chat = deepseek("sk", "deepseek-flash", max_tokens=500, transport=transport)
    assert chat([{"role": "user", "content": "hello"}]) == "hi"
    body = json.loads(sent[0].content)
    assert body["model"] == "deepseek-flash" and body["max_tokens"] == 500
    assert body["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize(
    "answer,error",
    [
        ({"status": 500}, "DeepSeek answered 500"),
        ({"finish": "length"}, "ran out of output tokens"),
        ({"raw": b"<html>"}, "wasn't the JSON expected"),
        ({"raw": b'{"choices": []}'}, "wasn't the JSON expected"),
    ],
)
def test_deepseek_errors(answer, error):
    _, transport = answering(**answer)
    with pytest.raises(LLMError, match=error):
        deepseek("sk", "deepseek-flash", transport=transport)([])


def test_deepseek_unreachable():
    def refuse(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError, match="couldn't reach DeepSeek: refused"):
        deepseek("sk", "deepseek-flash", transport=httpx.MockTransport(refuse))([])


def test_provider_sends_glm_to_the_coding_endpoint_with_the_current_key(
    tmp_path, monkeypatch
):
    for k in ("GENERIC_OPEN_AI_BASE_PATH", "GENERIC_OPEN_AI_API_KEY", "ZAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert (
        provider_for("GLM-5.3-flash") == "zai"
        and provider_for("deepseek-v4-pro") == "deepseek"
    )
    env = tmp_path / ".env"
    env.write_text(
        "GENERIC_OPEN_AI_BASE_PATH='https://api.z.ai/api/coding/paas/v4'\n"
        "GENERIC_OPEN_AI_API_KEY=new\nZAI_API_KEY=old\nDEEPSEEK_API_KEY=ds\n"
    )
    zai = provider("zai", str(env))
    assert (zai.base_url, zai.key, zai.key_name) == (
        "https://api.z.ai/api/coding/paas/v4",
        "new",
        "GENERIC_OPEN_AI_API_KEY",
    )
    assert provider("deepseek", str(env)).key == "ds"
    env.write_text(
        "GENERIC_OPEN_AI_BASE_PATH=http://localhost:8080/v1\nZAI_API_KEY=old\n"
    )
    assert provider("zai", str(env)).key == "old"
    assert provider("deepseek", str(env)).key == ""


def completions(transport):
    return Completions(
        Provider("zai", "https://api.example/v4", "k", "ZAI_API_KEY"),
        transport=transport,
        backoff=0,
    )


def test_completions_returns_text_and_usage_and_turns_thinking_off():
    sent, transport = answering(usage={"prompt_tokens": 3})
    assert completions(transport).create(
        "m", [{"role": "user", "content": "x"}], 99, think=False
    ) == ("hi", {"prompt_tokens": 3})
    assert str(sent[0].url) == "https://api.example/v4/chat/completions"
    assert sent[0].headers["authorization"] == "Bearer k"
    assert json.loads(sent[0].content) == {
        "model": "m",
        "messages": [{"role": "user", "content": "x"}],
        "max_tokens": 99,
        "thinking": {"type": "disabled"},
    }


def test_completions_errors_carry_status_and_code():
    quota = b'{"error": {"code": "1113", "message": "Insufficient balance"}}'
    with pytest.raises(APIError) as e:
        completions(answering(400, raw=quota)[1]).create("glm-5.3", [], 10)
    assert (e.value.status, e.value.code) == (400, "1113")
    assert "Insufficient balance" in str(e.value)

    cut = answering(finish="length", content="half")[1]
    with pytest.raises(LLMError, match=r"ran out of output tokens \(max_tokens 10\)"):
        completions(cut).create("m", [], 10)


def test_completions_retry_server_errors_but_not_429():
    sent, flaky = answering((503, 503, 200), content="ok")
    assert completions(flaky).create("m", [], 10)[0] == "ok" and len(sent) == 3

    sent, spent = answering(429, raw=b'{"error": {"message": "Usage limit reached"}}')
    with pytest.raises(APIError) as e:
        completions(spent).create("m", [], 10)
    assert e.value.status == 429 and len(sent) == 1
