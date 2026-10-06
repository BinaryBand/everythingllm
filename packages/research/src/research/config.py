"""Depth presets and shared limits for a research run."""

# `searches` caps a run's web searches. A 108-search thorough run got Google CSE,
# Brave and DuckDuckGo to block this server, which also breaks the news job.
DEPTHS = {
    "quick": {
        "workers": 3,
        "steps": 5,
        "gap_rounds": 0,
        "gap_workers": 0,
        "searches": 15,
    },
    "standard": {
        "workers": 5,
        "steps": 8,
        "gap_rounds": 1,
        "gap_workers": 3,
        "searches": 40,
    },
    "thorough": {
        "workers": 8,
        "steps": 12,
        "gap_rounds": 2,
        "gap_workers": 4,
        "searches": 80,
    },
}
DEFAULT_DEPTH = "standard"

# In flight at once across all workers. Searches go one at a time with a gap
# between them (web.make_search), since the engines behind SearXNG block bursts; page
# reads and model calls stay parallel, so this costs a run little time.
LIMITS = {"llm": 8, "fetch": 4}
SEARCH_GAP = 2.0  # seconds

PAGE_CHARS = 12_000  # page text handed to the extraction call
CHECK_CHARS = 400_000  # page text a quote is looked for in (the agents engine)
NOTES_PER_WORKER = 30
FINDINGS_PER_PAGE = 8
RESULTS_PER_SEARCH = 8

# Output budgets. The planner thinks before answering, and its reasoning tokens count
# against max_tokens, so these leave plenty of room.
MAX_TOKENS = {"json": 16_000, "worker": 4_000, "write": 64_000, "verify": 64_000}


def depth_preset(depth: str | None) -> dict:
    key = str(depth or "").strip().lower()
    return {
        "name": key if key in DEPTHS else DEFAULT_DEPTH,
        **DEPTHS.get(key, DEPTHS[DEFAULT_DEPTH]),
    }
