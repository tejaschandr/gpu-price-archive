"""HTML-to-text helpers. Stdlib only; tolerant of layout changes."""

from __future__ import annotations

import html
import re
from datetime import datetime
from html.parser import HTMLParser

_MONTHS = ("january february march april may june july august september "
           "october november december").split()


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.meta: dict[str, str] = {}
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "meta":
            a = dict(attrs)
            key = a.get("name") or a.get("property")
            if key and "content" in a:
                self.meta[key.lower()] = a["content"] or ""
        if tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "section", "td", "th", "span"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(body: bytes | str) -> tuple[str, dict[str, str]]:
    """Return (visible text with collapsed whitespace, meta tags)."""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    p = _Text()
    p.feed(body)
    text = html.unescape(" ".join(p.parts))
    text = re.sub(r"\s+", " ", text).strip()
    return text, p.meta


def parse_date(s: str) -> str:
    """'October 13, 2026' / 'Oct 12, 2026' -> '2026-10-13'. Returns '' if unparseable."""
    s = s.strip().replace(",", "")
    parts = s.split()
    if len(parts) != 3:
        return ""
    mon, day, year = parts
    mon = mon.lower().rstrip(".")
    idx = next((i for i, m in enumerate(_MONTHS) if m.startswith(mon[:3])), None)
    if idx is None:
        return ""
    try:
        return datetime(int(year), idx + 1, int(day)).strftime("%Y-%m-%d")
    except ValueError:
        return ""


DATE_RE = r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? \d{1,2}, \d{4})"


class _Table(HTMLParser):
    """Collect every <table> as a list of rows, each row a list of cell texts (th and td)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._cell: list[str] | None = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _Text.SKIP:
            self._skip += 1
        elif tag == "table":
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self.tables[-1].append([])
        elif tag in ("td", "th") and self.tables and self.tables[-1]:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in _Text.SKIP and self._skip:
            self._skip -= 1
        elif tag in ("td", "th") and self._cell is not None:
            self.tables[-1][-1].append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None and not self._skip:
            self._cell.append(data)


def table_with(body: bytes | str, header_text: str, lead: list[str]) -> tuple[list[str], list[list[str]], int] | None:
    """The one table whose first row starts with the cells in lead and has exactly one cell containing header_text:
    (header, rows, column index). None if no table matches, or more than one table or
    column does: an ambiguous match is not resolved by position."""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    p = _Table()
    p.feed(body)
    p.close()
    hits = [t for t in p.tables if t and t[0][:len(lead)] == lead and any(header_text in c for c in t[0])]
    if len(hits) != 1:
        return None
    cols = [i for i, c in enumerate(hits[0][0]) if header_text in c]
    if len(cols) != 1:
        return None
    return hits[0][0], hits[0][1:], cols[0]
