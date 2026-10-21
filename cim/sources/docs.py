"""Vendor methodology, terms and announcement pages: what each source says it measures and allows.
A new version of a page's visible text is a new `documents` row; each Silicon Data announcement
(index launch, methodology change, restatement) is one `announcements` row, revised when edited.
Restatements of history older than Silicon Data's 30-day page window show up only here.

Not covered: Silicon Data's terms (the text loads by script; the page holds only navigation) and
Kalshi's terms (behind a browser check). Kalshi's contract rules are already in every market's
`rules_primary` in the archived API responses.
"""

from __future__ import annotations

import hashlib
import re

from ..model import Request, Rows
from ..text import html_to_text

NAME = "docs"
PAGES = {
    "ornn_methodology": "https://data.ornn.com/methodology",
    "ornn_terms": "https://data.ornn.com/terms",
    "silicondata_announcements": "https://docs.silicondata.com/products/gpu-index-announcements.md",
    "vast_terms": "https://vast.ai/terms",
}
EXPECTED_ROWS = {"documents": len(PAGES), "announcements": 1}

# Site navigation and footers change with every redesign (Ornn's, 2026-10-21); only the content counts.
_CHROME = re.compile(rb"<(nav|header|footer)\b.*?</\1\s*>", re.S | re.I)
_UPDATE = re.compile(r'<Update label="([^"]+)" description="([^"]*)">(.*?)</Update>', re.S)
# Since 2026-10-20 the page renders each entry as a bold label, entries separated by "***", with no
# description; the body text is the same, so its hash carries over.
_LABEL = re.compile(r"^\*\*(\d{6}-\d+)\*\*[ \t]*$", re.M)
_RULE = re.compile(r"^\*{3}[ \t]*$", re.M)
_FOOTER = re.compile(r"\n\s*This documentation is built and hosted on .*\Z", re.S)


def _entries(text: str) -> list[tuple[str, str, str]]:
    """(label, description, body) for each announcement, in either page format."""
    old = _UPDATE.findall(text)
    if old:
        return old
    marks = list(_LABEL.finditer(text))
    return [(m.group(1), "", _FOOTER.sub("", _RULE.split(text[m.end():nxt.start() if nxt else len(text)])[0]))
            for m, nxt in zip(marks, marks[1:] + [None])]


def _field(block: str, name: str) -> str:
    m = re.search(rf"^\s*\*\s*{name}:[ \t]*(\S[^\n]*?)\s*$", block, re.M)
    return m.group(1) if m else ""


def requests() -> tuple[list[Request], list[str]]:
    return [Request(n, u, "md" if u.endswith(".md") else "html") for n, u in PAGES.items()], []


def parse(name: str, body: bytes, url: str) -> tuple[Rows, list[str]]:
    text = body.decode("utf-8", "replace") if url.endswith(".md") else html_to_text(_CHROME.sub(b"", body))[0]
    words = " ".join(text.split())
    if len(words) < 500:  # a challenge page or a script-only shell: nothing to version
        return {}, [f"{name}: only {len(words)} characters of text"]
    doc = {"name": name, "url": url, "chars": len(words),
           "text_sha256": hashlib.sha256(words.encode()).hexdigest()}
    rows: Rows = {"documents": [doc]}
    if name != "silicondata_announcements":
        return rows, []
    rows["announcements"] = [
        {"label": label, "description": desc, "announced": _field(b, "Announcement Date"),
         "effective": _field(b, "Effective Date"), "type": _field(b, "Type"),
         "restatement": _field(b, "Restatement"),
         "body_sha256": hashlib.sha256(" ".join(b.split()).encode()).hexdigest()}
        for label, desc, b in _entries(text)]
    return rows, [] if rows["announcements"] else ["silicondata_announcements: no announcement entries found"]
