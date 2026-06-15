"""Split an episode's raw content into coherent chunks for fact extraction.

Markdown is split by headings (carrying the heading path as the locator); plain
text by paragraphs grouped to a target size; short input stays a single chunk.
Each chunk is (locator, text) — locator records *where* in the source it came
from, which becomes a fact's provenance.
"""

from __future__ import annotations

import re

TARGET = 1400      # aim for chunks around this many chars
SHORT = 400        # below this, don't bother chunking — one fact
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def chunk(content: str, source_type: str = "note") -> list[tuple[str, str]]:
    text = content.strip()
    if not text:
        return []
    # notes / hotkey grabs: a single fact, no splitting
    if source_type in ("note", "hotkey"):
        return [("", text)]
    # markdown always splits by headings (even when short) so the `#` and section
    # structure never leak into a fact
    if _looks_markdown(text):
        return _chunk_markdown(text)
    # short plain text: one fact; longer prose: split into paragraphs
    if len(text) < SHORT:
        return [("", text)]
    return _chunk_plain(text)


def _looks_markdown(text: str) -> bool:
    return any(HEADING.match(ln) for ln in text.splitlines())


def _chunk_markdown(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, list[str]]] = [("", [])]
    path: list[str] = []
    for line in text.splitlines():
        m = HEADING.match(line)
        if m:
            level = len(m.group(1))
            path = path[: level - 1] + [m.group(2).strip()]
            sections.append((" › ".join(path), []))
        else:
            sections[-1][1].append(line)
    out: list[tuple[str, str]] = []
    for locator, lines in sections:
        body = "\n".join(lines).strip()
        if not body:
            continue
        # sub-split sections that are too long
        for i, piece in enumerate(_split_long(body)):
            loc = locator + (f" ({i+1})" if i else "")
            out.append((loc, piece))
    return out or [("", text)]


def _chunk_plain(text: str) -> list[tuple[str, str]]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out, buf = [], ""
    for p in paras:
        if buf and len(buf) + len(p) > TARGET:
            out.append(("", buf)); buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        out.append(("", buf))
    return out or [("", text)]


def _split_long(body: str) -> list[str]:
    if len(body) <= TARGET:
        return [body]
    return [c for _, c in _chunk_plain(body)]
