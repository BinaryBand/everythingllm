import asyncio
import json

import httpx
import pytest
from relay import upstream

KEY = "allm-key-0123456789"


def data(**chunk):
    return f"data: {json.dumps(chunk)}\n\n"


def events(*, status=200, body="", error=None, seen=None):
    """relay.upstream.answer's events for one stream-chat reply."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if error:
            raise error
        return httpx.Response(status, content=body.encode())

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [
                e
                async for e in upstream.answer(
                    client, "http://allm/", KEY, "my space", "t/1", "Hi?", "query"
                )
            ]

    return asyncio.run(main())


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
    assert json.loads(request.content) == {"message": "Hi?", "mode": "query"}


def test_pieces_then_done_with_the_last_non_empty_sources():
    body = (
        ": comment\n"
        "event: something\n"
        + data(textResponse="Hel", sources=[], error=None, close=False)
        + "data: not json\n"
        + "data: [1, 2]\n"
        + data(textResponse="", sources=[{"title": "A"}, {"title": "B"}])
        + data(textResponse="lo", sources=[{"title": "C"}, "junk", {"no": "title"}])
        + data(sources=[])
        + data(textResponse="", close=True)
        + data(textResponse="after close")
    )
    assert events(body=body) == [
        ("text", {"text": "Hel"}),
        ("text", {"text": "lo"}),
        ("done", {"citations": ["C"]}),
    ]


def test_a_stream_that_just_ends_is_done_with_what_it_had():
    body = data(textResponse="x", sources=[{"title": "A"}])
    assert events(body=body) == [
        ("text", {"text": "x"}),
        ("done", {"citations": ["A"]}),
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
    assert events(body=body)[-1] == ("failed", {"error": error})


def test_a_blank_or_false_error_is_not_one():
    body = data(error=False) + data(error="  ") + data(textResponse="ok", close=True)
    assert events(body=body) == [("text", {"text": "ok"}), ("done", {"citations": []})]


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 400])
def test_a_non_2xx_status_fails_in_plain_language(status):
    [event] = events(status=status, body='{"error": "x"}')
    assert event == ("failed", {"error": upstream.status_error(status)})
    assert str(status) in event[1]["error"] or status in (401, 403, 404, 429)


def test_an_unreachable_anythingllm_fails_the_run():
    [event] = events(error=httpx.ConnectError("refused"))
    assert event == ("failed", {"error": upstream.UNREACHABLE})
