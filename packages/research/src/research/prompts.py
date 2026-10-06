"""Prompts for each stage. Every JSON prompt spells out the exact shape expected."""


def sys(content: str) -> dict:
    return {"role": "system", "content": content}


def user(content: str) -> dict:
    return {"role": "user", "content": content}


def plan(question: str, today: str, count: int) -> list[dict]:
    return [
        sys(
            f"""You are the lead researcher planning a web research project. Today is {today}.
Split the question into exactly {count} sub-questions that together cover it: distinct facets, no overlap, each answerable from web sources. Order them by importance.
For each, give 2-3 web search queries a person would type (short, specific, varied wording).
Reply with JSON only:
{{"title": "a short report title", "sub_questions": [{{"goal": "what this part must find out", "queries": ["...", "..."]}}]}}"""
        ),
        user(question),
    ]


def step(
    question: str,
    goal: str,
    today: str,
    steps_left: int,
    searches_left: int,
    queries: list[str],
    results: list[dict],
    notes: list[dict],
    read_urls: list[str],
) -> list[dict]:
    # Ordered for the provider's prefix cache: what's the same on every step first (the
    # system message, question and goal), then the lists that only grow (notes, pages read,
    # searches done), and what changes every step (latest results, counters) last.
    result_list = (
        "\n".join(
            f"{i + 1}. {r['title']}\n   {r['url']}\n   {r['snippet'][:300]}"
            for i, r in enumerate(results)
        )
        if results
        else "(none yet)"
    )
    note_list = (
        "\n".join(f"- {n['claim']} [{n['source_id']}]" for n in notes)
        if notes
        else "(none yet)"
    )
    searches = (
        f"Searches are shared with other researchers; {searches_left} remain, so search only when the results so far won't do."
        if searches_left > 0
        else "No searches remain: read pages from the results you have, or finish."
    )
    done = ", ".join(f'"{q}"' for q in queries) if queries else "(none)"
    pages = ", ".join(read_urls) if read_urls else "(none)"
    return [
        sys(
            f"""You are a research assistant working on one part of a larger research question. Today is {today}.
Each turn you choose one action:
- {{"action": "search", "query": "..."}} to run a web search
- {{"action": "read", "url": "..."}} to read a page from the latest results; its facts get added to your notes
- {{"action": "done", "summary": "..."}} when your notes answer the goal well or nothing more is likely to turn up
Prefer primary and authoritative sources (official sites, papers, filings, reputable outlets) and recent ones when timing matters. Read pages, don't rely on snippets. Don't repeat a search or re-read a page. Look for disagreement between sources.
Read at least 3 pages from different sites before finishing, unless the searches turn up nothing relevant.
Reply with one JSON object only, with the key "action"."""
        ),
        user(
            f"""Overall question: {question}
Your goal: {goal}

Your notes so far:
{note_list}

Pages read: {pages}
Searches done: {done}

Latest search results:
{result_list}

You have read {len(read_urls)} pages and have {steps_left} actions left. {searches}"""
        ),
    ]


def extract(
    question: str, goal: str, url: str, title: str, text: str, max: int
) -> list[dict]:
    return [
        sys(
            f"""You extract facts from a web page for a research project.
Return up to {max} findings relevant to the goal. Each finding is a claim in your own words plus a quote copied exactly, character for character, from the page that supports it (one or two sentences, no paraphrasing; findings with altered quotes are discarded).
Include numbers, dates, names and who said what. Skip navigation, ads and anything off-topic. If nothing on the page is relevant, return an empty list.
Reply with JSON only:
{{"page_title": "...", "findings": [{{"claim": "...", "quote": "..."}}]}}"""
        ),
        user(
            f"""Overall question: {question}
Goal: {goal}
Page: {title} ({url})

--- page text ---
{text}"""
        ),
    ]


def gaps(question: str, today: str, sections: str, max: int) -> list[dict]:
    return [
        sys(
            f"""You are the lead researcher reviewing your team's notes. Today is {today}.
Find what is still missing to answer the question well: unanswered parts, claims resting on a single weak source, contradictions between sources, and outdated information.
Propose at most {max} follow-up tasks, each with 2-3 search queries. Propose none if the notes already cover the question well.
Reply with JSON only:
{{"assessment": "two or three sentences", "follow_ups": [{{"goal": "...", "queries": ["...", "..."]}}]}}"""
        ),
        user(f"Question: {question}\n\n{sections}"),
    ]


def write(question: str, today: str, findings: str) -> list[dict]:
    return [
        sys(
            f"""You are writing a research report from your team's verified notes. Today is {today}.
Use only facts in the notes. Cite every factual sentence with the source number(s) in square brackets, like [3] or [3, 7], using the numbers in the notes. Never invent a source number.
Where sources disagree, say so and cite both. Say plainly where evidence is thin or missing.
Structure, in Markdown:
## Summary
5-8 bullet points with the key answers, cited.
## <section headings of your choice>
The detailed findings, organized by theme, in prose with tables where they help.
## Open questions
What the sources don't settle.
Don't add a title line or a source list; both are added later. Write in the language of the question."""
        ),
        user(f"Question: {question}\n\nNotes by source:\n\n{findings}"),
    ]


def verify(report: str, findings: str) -> list[dict]:
    return [
        sys(
            """You are fact-checking a research report against the notes it was written from. Each [n] cites the source numbered n in the notes.
Find sentences whose claim is not supported by the notes of the source(s) they cite: wrong numbers, overstated certainty, facts from nowhere. Ignore style.
For each, give the exact sentence as it appears in the report and a corrected version that the notes support (keep the citations), or an empty string to delete it.
Reply with JSON only:
{"edits": [{"find": "exact sentence from the report", "replace": "corrected sentence, or empty"}]}"""
        ),
        user(f"Notes by source:\n\n{findings}\n\n--- report ---\n{report}"),
    ]
