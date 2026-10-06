import asyncio
import base64
import json

import httpx
import pytest
from relay import upstream

KEY = "allm-key-0123456789"


def data(**chunk):
    return f"data: {json.dumps(chunk)}\n\n"


def events(*, status=200, body="", error=None, seen=None, chat=None):
    """relay.upstream.answer's events for one stream-chat reply to `chat`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if error:
            raise error
        content = body.encode() if isinstance(body, str) else body
        return httpx.Response(status, content=content)

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [
                e
                async for e in upstream.answer(
                    client,
                    "http://allm/",
                    "my space",
                    "t/1",
                    chat or {"message": "Hi?"},
                    KEY,
                )
            ]

    return asyncio.run(main())


def chunks(*cs):
    return [("chunk", c) for c in cs]


def test_the_call_is_the_one_nilson_made():
    seen = []
    events(body=data(close=True), seen=seen)
    [request] = seen
    assert request.method == "POST"
    assert (
        str(request.url)
        == "http://allm/api/v1/workspace/my%20space/thread/t%2F1/stream-chat"
    )
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["accept"] == "text/event-stream"
    assert json.loads(request.content) == {"message": "Hi?"}  # no mode added


def test_the_body_goes_as_it_came():
    chat = {
        "message": "What's this?",
        "mode": "query",
        "attachments": [
            {
                "name": "a.png",
                "mime": "image/png",
                "contentString": "data:image/png;base64,iVBORw0KGgo=",
            }
        ],
    }
    seen = []
    events(body=data(close=True), seen=seen, chat=chat)
    assert json.loads(seen[0].content) == chat


def test_a_20_mb_attachment_is_forwarded_whole():
    content = base64.b64encode(b"\x89" * 20 * 1024 * 1024).decode()
    chat = {
        "message": "Read this",
        "attachments": [
            {
                "name": "big.pdf",
                "mime": "application/anythingllm-document",
                "contentString": f"data:application/pdf;base64,{content}",
            }
        ],
    }
    seen = []
    events(body=data(close=True), seen=seen, chat=chat)
    assert json.loads(seen[0].content) == chat


def test_an_agent_answer_comes_back_chunk_by_chunk_then_done():
    stream = [
        {"type": "agentThought", "thought": "Using SearXNG to search for x"},
        {"type": "textResponseChunk", "textResponse": "Hel", "close": False},
        {"type": "textResponseChunk", "textResponse": "lo", "close": False},
        {"type": "textResponse", "textResponse": "Hello", "close": True},
        {
            "type": "finalizeResponseStream",
            "close": True,
            "sources": [{"title": "x", "chunkSource": "link://https://x.example/"}],
        },
    ]
    body = (
        ": comment\n"
        "event: something\n"
        + data(**stream[0])
        + "data: not json\n"
        + "data: [1, 2]\n"
        + "".join(data(**c) for c in stream[1:])
    )
    assert events(body=body) == [*chunks(*stream), ("done", {})]


def test_a_web_source_keeps_its_address():
    close = {
        "type": "textResponseChunk",
        "textResponse": "",
        "close": True,
        "error": False,
        "sources": [
            {"title": "Page", "chunkSource": "link://https://example.com/a?b=c"},
            {"title": "Doc"},
        ],
    }
    final = {"type": "finalizeResponseStream", "close": True}
    assert events(body=data(**close) + data(**final)) == [
        *chunks(close, final),
        ("done", {}),
    ]


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (
            data(textResponse="pa") + data(error="  Model overloaded \n"),
            "Model overloaded",
        ),
        (
            data(textResponse="pa") + data(type="abort", textResponse=None),
            upstream.ABORTED,
        ),
    ],
)
def test_an_error_or_abort_chunk_fails_the_run(body, error):
    assert events(body=body) == [
        ("chunk", {"textResponse": "pa"}),
        ("failed", {"error": error}),
    ]


def test_a_blank_or_false_error_is_not_one():
    body = data(error=False) + data(error="  ") + data(textResponse="ok", close=True)
    assert events(body=body) == [
        *chunks(
            {"error": False}, {"error": "  "}, {"textResponse": "ok", "close": True}
        ),
        ("done", {}),
    ]


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 400])
def test_a_non_2xx_status_fails_in_plain_language(status):
    [event] = events(status=status, body='{"error": "x"}')
    assert event == ("failed", {"error": upstream.status_error(status)})
    assert str(status) in event[1]["error"] or status in (401, 403, 404, 429)


def test_an_unreachable_anythingllm_fails_the_run():
    [event] = events(error=httpx.ConnectError("refused"))
    assert event == ("failed", {"error": upstream.UNREACHABLE})


def test_a_connection_that_breaks_mid_answer_fails_the_run():
    class Breaks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield data(textResponse="pa").encode()
            raise httpx.ReadError("gone")

    assert events(body=Breaks()) == [
        ("chunk", {"textResponse": "pa"}),
        ("failed", {"error": upstream.BROKEN}),
    ]
