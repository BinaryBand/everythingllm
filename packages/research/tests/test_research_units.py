import threading
import time
from itertools import pairwise

import httpx
import pytest
from publicweb.pages import SearchError, make_image_search, make_search
from research.config import depth_preset
from research.llm import LLM, cached_tokens, out_of_quota
from research.pipeline import apply_edits, tasks_from
from research.sources import (
    SourceList,
    finalize_citations,
    quote_in_text,
    summary_bullets,
)
from research.web import make_reader


class Fake:
    """A client like llm.Completions that answers from a script: text, or an exception to raise."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, model, messages, max_tokens, think=True):
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "think": think,
            }
        )
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r, {
            "prompt_tokens": 10,
            "prompt_cache_hit_tokens": 6,
            "completion_tokens": 5,
            "completion_tokens_details": {"reasoning_tokens": 2},
        }


class Refused(Exception):
    def __init__(self, message, status=None, code=""):
        super().__init__(message)
        self.status, self.code = status, code


def test_llm_json_repairs_a_bad_reply_once_and_counts_usage():
    client = Fake(["oops", '{"ok": true}'])
    llm = LLM(client)
    assert llm.json("m", [{"role": "user", "content": "hi"}], think=False) == {
        "ok": True
    }
    assert len(client.calls) == 2 and client.calls[0]["think"] is False
    assert llm.usage == {
        "calls": 2,
        "prompt": 20,
        "cached": 12,
        "completion": 10,
        "reasoning": 4,
    }


def test_cached_tokens_read_both_providers_shapes():
    assert cached_tokens({"prompt_cache_hit_tokens": 7}) == 7
    assert cached_tokens({"prompt_tokens_details": {"cached_tokens": 3}}) == 3
    assert cached_tokens({}) == 0


def test_llm_json_wants_an_object():
    llm = LLM(Fake(["[1, 2]", "still [3]"]))
    with pytest.raises(ValueError):
        llm.json("m", [])


def test_llm_routes_each_call_to_its_models_client():
    glm, deepseek = Fake(["plan"]), Fake(["notes"])
    llm = LLM(lambda model: glm if model.startswith("glm-") else deepseek)
    assert llm.chat("glm-5.3", []) == "plan"
    assert llm.chat("deepseek-flash", []) == "notes"
    assert [glm.calls[0]["model"], deepseek.calls[0]["model"]] == [
        "glm-5.3",
        "deepseek-flash",
    ]


def test_a_planner_out_of_quota_switches_to_its_fallback_for_the_rest_of_the_run():
    glm = Fake([Refused("429 Usage limit reached for 5 hour", status=429)])
    deepseek = Fake(["plan", "report"])
    switches = []
    llm = LLM(
        lambda model: glm if model.startswith("glm-") else deepseek,
        fallback={"glm-5.3": "deepseek-flash"},
        on_fallback=lambda frm, to, e: switches.append((frm, to)),
    )
    assert llm.chat("glm-5.3", [], max_tokens=99) == "plan"
    assert llm.chat("glm-5.3", []) == "report"
    assert len(glm.calls) == 1, "no more calls to a spent plan"
    assert [c["model"] for c in deepseek.calls] == ["deepseek-flash", "deepseek-flash"]
    assert deepseek.calls[0]["max_tokens"] == 99
    assert switches == [("glm-5.3", "deepseek-flash")]
    assert llm.switched == {"glm-5.3": "deepseek-flash"}


def test_only_a_quota_error_falls_back():
    assert out_of_quota(Refused("Insufficient balance", status=400, code="1113"))
    assert not out_of_quota(Refused("boom", status=500))
    llm = LLM(
        Fake([Refused("500 boom", status=500)]), fallback={"glm-5.3": "deepseek-flash"}
    )
    with pytest.raises(Refused, match="boom"):
        llm.chat("glm-5.3", [])
    assert llm.switched == {}


def test_llm_never_has_more_than_eight_calls_in_flight():
    active, peak, lock = [0], [0], threading.Lock()

    class Slow:
        def create(self, model, messages, max_tokens, think=True):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.02)
            with lock:
                active[0] -= 1
            return "x", {}

    llm = LLM(Slow())
    threads = [threading.Thread(target=llm.chat, args=("m", [])) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 8 and llm.usage["calls"] == 20


def test_quote_in_text_ignores_case_whitespace_and_curly_quotes_but_not_rewording():
    page = "The plant produced  4.2 TWh in 2024,\nup from 3.9 TWh. “It’s a record,” the CEO said."
    assert quote_in_text("the plant produced 4.2 TWh in 2024", page)
    assert quote_in_text('"It\'s a record," the CEO said', page)
    assert quote_in_text("The plant produced 4.2 TWh ... up from 3.9 TWh", page)
    assert not quote_in_text("The plant produced 5 TWh in 2024", page)
    assert not quote_in_text("TWh", page), "too short to verify anything"
    assert not quote_in_text("", page)
    assert quote_in_text("costs 5 000 euros a year", "It costs 5 000 euros a year.")


def test_finalize_citations_renumbers_by_first_use_drops_unknown_ids_and_lists_sources():
    sources = SourceList()
    sources.add("https://a.example/", "A")
    sources.add("https://b.example/", "B [draft]")
    sources.add("https://c.example/", "C")
    markdown, used = finalize_citations(
        "First [3]. Second [2, 3]. Bogus [9]. Pair [2][1]. A [link](https://x.example) and [1](https://y.example).",
        sources,
    )
    assert markdown.split("\n\n## Sources")[0] == (
        "First [1]. Second [2][1]. Bogus. Pair [2][3]. A [link](https://x.example) and [1](https://y.example)."
    )
    assert [(s["id"], s["url"]) for s in used] == [
        (1, "https://c.example/"),
        (2, "https://b.example/"),
        (3, "https://a.example/"),
    ]
    assert (
        "## Sources\n\n1. [C](https://c.example/)\n2. [B draft](https://b.example/)\n3. [A]"
        in markdown
    )


def test_source_list_reuses_ids_per_url():
    s = SourceList()
    assert s.add("https://a/", "")["id"] == 1
    assert s.add("https://b/", "B")["id"] == 2
    again = s.add("https://a/", "Better title")
    assert again["id"] == 1 and again["title"] == "Better title"


def test_summary_bullets_reads_the_summary_section_only():
    md = "## Summary\n\n- One [1]\n- Two\n\n## Details\n\n- Not this"
    assert summary_bullets(md) == ["One [1]", "Two"]
    assert summary_bullets("## Other\n- x") == []
    assert summary_bullets("## Executive summary\n* Last one") == ["Last one"]


def test_apply_edits_replaces_exact_sentences_and_skips_the_rest():
    text, applied = apply_edits(
        "Alpha is 5 units [1]. Beta grew 10% [2].\n\n\n\nEnd.",
        [
            {"find": "Alpha is 5 units [1].", "replace": "Alpha is 4 units [1]."},
            {"find": "Beta grew 10% [2].", "replace": ""},
            {"find": "Not in the report at all.", "replace": "x"},
            {"find": "short", "replace": "x"},
            "not an edit",
        ],
    )
    assert applied == 2
    assert text == "Alpha is 4 units [1]. \n\nEnd."


def test_tasks_from_cleans_and_caps_planner_output():
    tasks = tasks_from(
        [
            {"goal": " A ", "queries": ["q1", "", "q2", "q3", "q4"]},
            {"queries": ["x"]},
            {"goal": "B"},
        ],
        5,
    )
    assert tasks == [
        {"goal": "A", "queries": ["q1", "q2", "q3"]},
        {"goal": "B", "queries": []},
    ]
    assert tasks_from(None, 3) == []


def test_depth_preset_falls_back_to_standard():
    assert depth_preset("Quick")["name"] == "quick"
    assert depth_preset(None)["name"] == "standard"
    assert depth_preset("bogus")["workers"] == 5
    assert depth_preset("thorough")["searches"] == 80


def searxng(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_make_search_runs_one_search_at_a_time_spaced_out_and_dedupes_results():
    starts, active, peak, lock = [], [0], [0], threading.Lock()

    def handler(request):
        with lock:
            starts.append(time.monotonic())
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.005)
        with lock:
            active[0] -= 1
        q = request.url.params["q"]
        assert request.url.params["format"] == "json"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": f"https://x/{q}", "title": q, "content": "s"},
                    {"url": f"https://x/{q}"},
                    {"url": "ftp://nope"},
                ]
            },
        )

    search = make_search("https://searx-a/search", searxng(handler), gap=0.04)
    results = {}
    threads = [
        threading.Thread(target=lambda q=q: results.__setitem__(q, search(q)))
        for q in "abc"
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert {q: [r["url"] for r in rs] for q, rs in results.items()} == {
        q: [f"https://x/{q}"] for q in "abc"
    }
    assert results["a"][0] == {"title": "a", "url": "https://x/a", "snippet": "s"}
    assert peak[0] == 1
    starts.sort()
    assert all(b - a >= 0.038 for a, b in pairwise(starts))


def test_two_runs_searches_share_one_queue_and_gap():
    starts = []

    def handler(request):
        starts.append(time.monotonic())
        return httpx.Response(200, json={"results": []})

    a = make_search("https://searx-shared/search", searxng(handler), gap=0.04)
    b = make_search("https://searx-shared/search", searxng(handler), gap=0.04)
    threads = [
        threading.Thread(target=f, args=(q,))
        for f, q in ((a, "one"), (b, "two"), (a, "three"), (b, "four"))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    starts.sort()
    assert all(y - x >= 0.038 for x, y in pairwise(starts))


def test_make_search_fails_naming_the_engines_when_they_refused_us():
    refused = searxng(
        lambda r: httpx.Response(
            200,
            json={
                "results": [],
                "unresponsive_engines": [
                    ["google cse", "Suspended: too many requests"]
                ],
            },
        )
    )
    with pytest.raises(
        SearchError,
        match=r"engines unavailable: google cse \(Suspended: too many requests\)",
    ):
        make_search("https://searx-b/search", refused, gap=0)("q")
    empty = searxng(lambda r: httpx.Response(200, json={"results": []}))
    assert make_search("https://searx-c/search", empty, gap=0)("q") == []
    down = searxng(lambda r: httpx.Response(502))
    with pytest.raises(SearchError, match='SearXNG answered 502 for "q"'):
        make_search("https://searx-d/search", down, gap=0)("q")


def test_make_image_search_asks_for_safe_pictures_and_keeps_each_picture_once():
    asked = []

    def handler(request):
        asked.append(dict(request.url.params))
        pic = {"url": "https://zoo.example/a", "img_src": "https://zoo.example/a.jpg"}
        return httpx.Response(
            200,
            json={
                "results": [
                    {**pic, "title": "A", "thumbnail_src": "https://t.example/a.jpg"},
                    pic,  # the same picture from another engine
                    {"url": "javascript:x", "img_src": "data:image/png;base64,AA"},
                    {"img_src": "https://zoo.example/b.png"},  # no page: the picture
                ]
            },
        )

    found = make_image_search("https://searx-i/search", searxng(handler), gap=0)("q")
    assert asked == [
        {"q": "q", "format": "json", "categories": "images", "safesearch": "1"}
    ]
    assert found == [
        {
            "title": "A",
            "page": "https://zoo.example/a",
            "thumb": "https://t.example/a.jpg",
            "full": "https://zoo.example/a.jpg",
        },
        {
            "title": "",
            "page": "https://zoo.example/b.png",
            "thumb": "",
            "full": "https://zoo.example/b.png",
        },
    ]


def test_make_reader_reads_each_page_once_and_caps_its_length():
    fetched = []

    def fetch(url):
        fetched.append(url)
        if url != "https://a/":
            raise OSError("down")
        return "  " + "x" * 50 + "  "

    read = make_reader(fetch=fetch, max_chars=10)
    assert read("https://a/") == "x" * 10
    assert read("https://a/") == "x" * 10
    assert read("https://gone/") is None
    assert fetched == ["https://a/", "https://gone/"]
