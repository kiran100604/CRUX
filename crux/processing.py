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


class AnthropicProcessor:
    def __init__(self, model: str, api_key: str | None):
        import anthropic  # lazy, optional dependency

        self.model = model
        self._client = anthropic.Anthropic(api_key=api_key)

    def enrich(self, content: str) -> Enrichment:
        msg = self._client.messages.create(
            model=self.model,
            max_tokens=300,
            messages=[{"role": "user", "content": _PROMPT.format(
                types=", ".join(ITEM_TYPES), content=content[:6000])}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        return _parse(text, content)


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
