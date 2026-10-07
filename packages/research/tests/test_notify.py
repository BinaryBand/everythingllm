import asyncio

import httpx
from research import notify

CHAT = {"workspace": "career", "thread": "7"}


def test_the_message_names_the_run_and_its_chat_and_links_the_report():
    headers, body = notify.message(
        "dr-1", True, " Is   bitcoin\nworth it? " + "x" * 200, CHAT, "https://h/r/"
    )
    assert headers == {
        "Title": "Research ready",
        "Tags": "run=dr-1,workspace=career,thread=7",
        "Click": "https://h/r/",
    }
    assert body == ("Is bitcoin worth it? " + "x" * 200)[:120].encode()
    headers, _ = notify.message("dr-2", False, "q", CHAT, None)
    assert headers["Title"] == "Research failed" and "Click" not in headers


def test_publishing_posts_to_the_topic_and_keeps_its_url_out_of_the_log(caplog):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(500 if len(seen) > 1 else 200)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            publish = notify.publisher(client, "https://ntfy.example/secret-topic", "tk")
            await publish("dr-1", True, "q", CHAT, "https://h/r/")
            await publish("dr-2", True, "q", CHAT, None)

        def refused(request):
            raise httpx.ConnectError("no route to https://ntfy.example/secret-topic")

        async with httpx.AsyncClient(transport=httpx.MockTransport(refused)) as client:
            await notify.publisher(client, "https://ntfy.example/secret-topic")(
                "dr-3", False, "q", CHAT, None
            )

    asyncio.run(go())
    assert str(seen[0].url) == "https://ntfy.example/secret-topic"
    assert seen[0].headers["authorization"] == "Bearer tk"
    assert seen[0].headers["click"] == "https://h/r/"
    assert seen[0].content == b"q"
    assert "ntfy answered 500 for dr-2" in caplog.text
    assert "couldn't notify ntfy about dr-3: ConnectError" in caplog.text
    assert "secret-topic" not in caplog.text


def test_no_topic_no_publisher(monkeypatch):
    monkeypatch.delenv("NTFY_URL", raising=False)
    assert notify.from_env() is None
    monkeypatch.setenv("NTFY_URL", " ")
    assert notify.from_env() is None
