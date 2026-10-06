"""Sources, quote checks and citation numbering."""

import re

MIN_QUOTE = 12

_FOLD = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "′": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "″": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        " ": " ",
    }
)


def normalize(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower().translate(_FOLD)).strip()


def quote_in_text(quote: str | None, text: str | None) -> bool:
    """Whether a quote really appears in the page text (case and whitespace aside).
    A quote with "..." in it must match each part that's long enough to mean something."""
    page = normalize(text)
    parts = [
        re.sub(r"^[\"']|[\"']$", "", p).strip()
        for p in re.split(r"\s*(?:\.\.\.|…)\s*", normalize(quote))
    ]
    parts = [p for p in parts if len(p) >= MIN_QUOTE]
    return bool(parts) and all(p in page for p in parts)


class SourceList:
    """Every page a finding came from, numbered from 1 in the order first seen. Workers add
    to it from their own threads, one at a time (pipeline.Counts.lock)."""

    def __init__(self):
        self.by_url: dict[str, dict] = {}
        self.by_id: list[dict] = []  # source n at n - 1

    def add(self, url: str, title: str | None = "") -> dict:
        source = self.by_url.get(url)
        if source is None:
            source = {
                "id": len(self.by_id) + 1,
                "url": url,
                "title": str(title or "").strip() or url,
            }
            self.by_url[url] = source
            self.by_id.append(source)
        elif source["title"] == url and title:
            source["title"] = str(title).strip()
        return source

    def get(self, id: int) -> dict | None:
        return self.by_id[id - 1] if 0 < id <= len(self.by_id) else None


# [3], [3, 7] or [3][7], but not a Markdown link like [3](https://...).
CITE_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\](?!\()")


def finalize_citations(
    markdown: str | None, sources: SourceList
) -> tuple[str, list[dict]]:
    """Renumber a report's citations 1..n in order of first use, drop numbers that aren't
    sources, and append the source list. Returns the report and the sources it cites."""
    order: dict[int, int] = {}  # old id -> new number
    used: list[dict] = []

    def renumber(m: re.Match) -> str:
        nums: list[int] = []
        for raw in m.group(1).split(","):
            source = sources.get(int(raw.strip()))
            if not source:
                continue
            if source["id"] not in order:
                order[source["id"]] = len(order) + 1
                used.append({**source, "id": len(order)})
            n = order[source["id"]]
            if n not in nums:
                nums.append(n)
        return "".join(f"[{n}]" for n in nums)

    body = CITE_RE.sub(renumber, str(markdown or ""))
    # A dropped citation can leave "claim ." behind.
    body = re.sub(r"[ \t]+([.,;:])", r"\1", body).rstrip()
    if used:
        listing = "\n".join(
            f"{s['id']}. [{md_text(s['title'])}]({s['url']})" for s in used
        )
        body += f"\n\n## Sources\n\n{listing}\n"
    return body, used


def md_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\[\]]", "", str(text))).strip()[:200]


SUMMARY_RE = re.compile(
    r"^#{1,3}\s*(?:executive\s+)?summary\s*\n(.*?)(?=^#{1,3}\s|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def summary_bullets(markdown: str, max: int = 8) -> list[str]:
    """The bullet points under the report's "Summary" heading, for the chat reply."""
    m = SUMMARY_RE.search(str(markdown))
    if not m:
        return []
    bullets = [
        b.group(1).strip()
        for line in m.group(1).split("\n")
        if (b := re.match(r"^\s*[-*]\s+(.*)", line))
    ]
    return [b for b in bullets if b][:max]
