"""Enrichment: raw text -> {title, summary, type, tags}.

Real path uses Anthropic Haiku. The `fake` path is a cheap heuristic so capture
never hard-fails when there's no API key — the loop stays usable offline and the
item can be re-enriched later.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .models import ITEM_TYPES

_PROMPT = """You process captured snippets for a developer's context store.
Return ONLY minified JSON with keys: title, summary, type, tags.
- title: <= 8 words, no trailing punctuation
- summary: exactly one sentence
- type: one of {types}
- tags: 2-4 short lowercase tags

Snippet:
\"\"\"
{content}
\"\"\""""

_MULTI_PROMPT = """You extract durable, atomic facts from a section of a document
for a developer/company context store. Pull out each distinct decision,
constraint, architectural choice, or important reference — one fact each. Ignore
filler, TODOs, and prose with no lasting signal. If the section holds nothing
durable, return [].

Return ONLY a minified JSON array; each element has keys: title, summary, type, tags.
- title: <= 8 words, no trailing punctuation
- summary: exactly one sentence
- type: one of {types}
- tags: 2-4 short lowercase tags

Section:
\"\"\"
{content}
\"\"\""""

_JUDGE_PROMPT = """Two statements from a developer's knowledge base. Do they
directly contradict each other — i.e. they cannot both be true, or one clearly
updates/supersedes the other? Unrelated or merely similar statements do NOT count.
Return ONLY JSON: {{"contradicts": true|false, "reason": "<= 12 words"}}.

A: {a}
B: {b}"""


@dataclass
class Enrichment:
    title: str
    summary: str
    type: str
    tags: list[str]


class FakeProcessor:
    def enrich(self, content: str) -> Enrichment:
        clean = " ".join(content.split())
        first_sentence = re.split(r"(?<=[.!?])\s", clean)[0] if clean else "Untitled"
        words = first_sentence.split()
        title = " ".join(words[:8]) or "Untitled"
        return Enrichment(
            title=title.rstrip(".,;:"),
            summary=first_sentence[:200] or "No content.",
            type=_guess_type(clean),
            tags=_guess_tags(clean),
        )

    def extract_facts(self, content: str) -> list[Enrichment]:
        # Offline can't truly split a chunk; one fact per chunk. Multi-fact still
        # emerges across a document because chunking yields many sections.
        return [self.enrich(content)]

    def judge_contradiction(self, a: str, b: str):
        # Offline can't reason — signal "no judgment", caller falls back to similarity.
        return None, ""


class AnthropicProcessor:
    def __init__(self, model: str, api_key: str | None):
        import anthropic  # lazy, optional dependency

        self.model = model
        self._client = anthropic.Anthropic(api_key=api_key)

    def _call(self, prompt: str, max_tokens: int) -> str:
        msg = self._client.messages.create(
            model=self.model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")

    def enrich(self, content: str) -> Enrichment:
        text = self._call(_PROMPT.format(types=", ".join(ITEM_TYPES), content=content[:6000]), 300)
        return _parse(text, content)

    def extract_facts(self, content: str) -> list[Enrichment]:
        text = self._call(_MULTI_PROMPT.format(types=", ".join(ITEM_TYPES), content=content[:8000]), 1200)
        facts = _parse_many(text)
        return facts if facts else [self.enrich(content)]

    def judge_contradiction(self, a: str, b: str):
        """Return (contradicts: bool, reason: str). One statement may UPDATE the
        other — that counts as a contradiction worth surfacing."""
        text = self._call(_JUDGE_PROMPT.format(a=a[:600], b=b[:600]), 120)
        try:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            d = json.loads(m.group(0) if m else text)
            return bool(d.get("contradicts")), str(d.get("reason", ""))[:120]
        except Exception:
            return None, ""


def _parse(text: str, content: str) -> Enrichment:
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        data = json.loads(m.group(0) if m else text)
        t = data.get("type", "context")
        if t not in ITEM_TYPES:
            t = "context"
        tags = data.get("tags", [])[:4]
        return Enrichment(
            title=str(data.get("title", "Untitled"))[:80].rstrip(".,;:"),
            summary=str(data.get("summary", "")) or content[:160],
            type=t,
            tags=[str(x).lower() for x in tags],
        )
    except Exception:
        return FakeProcessor().enrich(content)


def _parse_many(text: str) -> list[Enrichment]:
    """Parse a JSON array of facts; tolerant of stray prose around it."""
    try:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        data = json.loads(m.group(0) if m else text)
        out = []
        for d in data:
            t = d.get("type", "context")
            if t not in ITEM_TYPES:
                t = "context"
            out.append(Enrichment(
                title=str(d.get("title", "Untitled"))[:80].rstrip(".,;:"),
                summary=str(d.get("summary", "")),
                type=t,
                tags=[str(x).lower() for x in d.get("tags", [])[:4]],
            ))
        return [e for e in out if e.title and e.title != "Untitled"]
    except Exception:
        return []


# --- heuristics for the offline path -----------------------------------------

_TYPE_HINTS = {
    "decision": ("decided", "chose", "we'll use", "going with", "rather than", "instead of"),
    "constraint": ("must", "never", "always", "required", "cannot", "do not", "ensure"),
    "architecture": ("architecture", "service", "schema", "pipeline", "component", "system"),
    "design": ("design", "ui", "ux", "layout", "flow", "wireframe"),
    "reference": ("http://", "https://", "see ", "docs", "reference"),
}


def _guess_type(text: str) -> str:
    low = text.lower()
    for typ, hints in _TYPE_HINTS.items():
        if any(h in low for h in hints):
            return typ
    return "context"


def _guess_tags(text: str) -> list[str]:
    words = re.findall(r"[a-z][a-z0-9]{3,}", text.lower())
    stop = {"this", "that", "with", "from", "have", "will", "they", "than", "into", "about", "because"}
    freq: dict[str, int] = {}
    for w in words:
        if w not in stop:
            freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda kv: -kv[1])[:3]]


def get_processor(cfg):
    if cfg.processing_provider == "anthropic":
        return AnthropicProcessor(cfg.processing_model, cfg.anthropic_api_key)
    return FakeProcessor()
