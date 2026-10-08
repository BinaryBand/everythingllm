import asyncio

import httpx
from research import notify

CHAT = {"workspace": "career", "thread": "7"}
TOPIC = "https://ntfy.example/secret-topic"


def test_the_message_names_the_run_and_its_chat_but_never_holds_the_report():
    question = " Is   bitcoin\nworth it? " + "x" * 200
    result = {"status": "ok", "file": "/s/research/r.md", "reply": "the report's text"}
    headers, body = notify.message("dr-1", question, CHAT, result)
    assert headers == {
        "Title": "Research ready",
        "Tags": "run=dr-1,workspace=career,thread=7",
    }
    assert body == ("Is bitcoin worth it? " + "x" * 200)[:120].encode()
    headers, _ = notify.message("dr-2", "q", CHAT, {"status": "failed"})
    assert headers["Title"] == "Research failed"


def published(respond, token=""):
    """The requests a publish to TOPIC made, its transport answering with respond."""
    seen = []

    def handler(request):
        seen.append(request)
        return respond(request)

    async def go():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            await notify.publisher(client, TOPIC, token)(
                "dr-1", "q", CHAT, {"status": "ok"}
            )

    asyncio.run(go())
    return seen


def test_publishing_posts_to_the_topic_with_its_token():
    [request] = published(lambda _: httpx.Response(200), token="tk")
    assert str(request.url) == TOPIC
    assert request.headers["authorization"] == "Bearer tk"
    assert request.content == b"q"


def test_a_refusal_or_a_broken_connection_is_logged_without_the_topic(caplog):
    published(lambda _: httpx.Response(500))
    assert "ntfy answered 500 for dr-1" in caplog.text

    def refused(_):
        raise httpx.ConnectError(f"no route to {TOPIC}")

    published(refused)
    assert "couldn't notify ntfy about dr-1: ConnectError" in caplog.text
    assert "secret-topic" not in caplog.text


def test_no_topic_no_publisher(monkeypatch):
    monkeypatch.delenv("NTFY_URL", raising=False)
    assert notify.from_env() is None
    monkeypatch.setenv("NTFY_URL", " ")
    assert notify.from_env() is None
