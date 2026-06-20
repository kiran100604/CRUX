"""Enrichment: raw text -> {title, summary, type, tags}.

Real path uses Anthropic Haiku. The `fake` path is a cheap heuristic so capture
never hard-fails when there's no API key — the loop stays usable offline and the
item can be re-enriched later.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .models import DOMAINS, ITEM_TYPES, TIERS

_TIER_GUIDE = """- tier: one of core | mid | leaf — the ALTITUDE of the fact (independent of type)
    core = company mission, vision, philosophy, the core problem & overall strategy
    mid  = product decisions, roadmap, planning, architecture choices
    leaf = granular operational facts, tasks, references, day-to-day work
- domain: one of product | technical | user | market | competitor | legal | process | other
    — what the fact is ABOUT (technical=architecture/code/infra, user=behaviour/feedback,
    market=positioning/go-to-market, process=team workflow/ops)"""

_PROMPT = """You process captured snippets for a developer/company context store.
Return ONLY minified JSON with keys: title, summary, type, tier, domain, tags.
- title: <= 8 words, no trailing punctuation
- summary: exactly one sentence
- type: one of {types}
{tier_guide}
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

Return ONLY a minified JSON array; each element has keys: title, summary, type, tier, domain, tags.
- title: <= 8 words, no trailing punctuation
- summary: exactly one sentence
- type: one of {types}
{tier_guide}
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

_RELATE_PROMPT = """You maintain a knowledge graph. A NEW fact arrived; classify
its relationship to an EXISTING fact. Pick exactly one:
- "updates": NEW contradicts or replaces EXISTING (they can't both be current).
- "extends": NEW adds detail to EXISTING; both stay true together (enrichment).
- "duplicate": NEW says essentially the same thing as EXISTING.
- "unrelated": different topics; no real link.
Return ONLY JSON: {{"relation": "...", "reason": "<= 12 words"}}.

NEW: {a}
EXISTING: {b}"""


_SUMMARIZE_PROMPT = """You maintain a running summary of what someone is working
on right now, so they (or an AI tool) can pick up the thread without re-explaining.
Given the GOAL and the STEPS taken so far (oldest first), write a tight status
brief with exactly these labels, each one line, omit a label if empty:
Goal: <the objective>
Tried: <approaches/things explored, comma-separated>
Now: <what they're focused on at this moment>
Next: <the obvious next step, if implied>
No preamble, no markdown, just those lines.

GOAL: {intent}

STEPS:
{steps}"""


@dataclass
class Enrichment:
    title: str
    summary: str
    type: str
    tags: list[str]
    tier: str = "leaf"
    domain: str = "other"


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
            tier=_guess_tier(clean),
            domain=_guess_domain(clean),
        )

    def extract_facts(self, content: str) -> list[Enrichment]:
        # Offline can't truly split a chunk; one fact per chunk. Multi-fact still
        # emerges across a document because chunking yields many sections.
        return [self.enrich(content)]

    def judge_contradiction(self, a: str, b: str):
        # Offline can't reason — signal "no judgment", caller falls back to similarity.
        return None, ""

    def classify_relation(self, a: str, b: str):
        # Offline can't reason — signal "no judgment", caller falls back to heuristic.
        return None, ""

    def summarize(self, intent: str, steps: list[str]) -> str:
        # Offline: no model to abstract with, so present the thread honestly as
        # Goal + the most recent steps, newest last. Still a usable, paste-able state.
        lines = []
        if intent:
            lines.append(f"Goal: {intent.strip()}")
        recent = [s.strip().replace("\n", " ") for s in steps if s.strip()][-6:]
        if recent:
            lines.append("Steps so far:")
            lines.extend(f"- {s[:160]}" for s in recent)
        return "\n".join(lines) if lines else (intent or "")


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
        text = self._call(_PROMPT.format(types=", ".join(ITEM_TYPES),
                                         tier_guide=_TIER_GUIDE, content=content[:6000]), 320)
        return _parse(text, content)

    def extract_facts(self, content: str) -> list[Enrichment]:
        text = self._call(_MULTI_PROMPT.format(types=", ".join(ITEM_TYPES),
                                               tier_guide=_TIER_GUIDE, content=content[:8000]), 1300)
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

    def classify_relation(self, a: str, b: str):
        """Return (relation, reason) where relation ∈
        updates | extends | duplicate | unrelated, or (None, '') if unparseable."""
        text = self._call(_RELATE_PROMPT.format(a=a[:600], b=b[:600]), 120)
        try:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            d = json.loads(m.group(0) if m else text)
            rel = str(d.get("relation", "")).lower().strip()
            if rel not in ("updates", "extends", "duplicate", "unrelated"):
                return None, ""
            return rel, str(d.get("reason", ""))[:120]
        except Exception:
            return None, ""

    def summarize(self, intent: str, steps: list[str]) -> str:
        """A living Goal/Tried/Now/Next brief over the thread's steps."""
        joined = "\n".join(f"- {s.strip()}" for s in steps if s.strip())[:6000] or "(none yet)"
        try:
            return self._call(_SUMMARIZE_PROMPT.format(intent=intent or "(unspecified)",
                                                       steps=joined), 320).strip()
        except Exception:
            # never let a summary failure break capture/view — fall back to raw
            return FakeProcessor.summarize(self, intent, steps)


class OpenAICompatProcessor(AnthropicProcessor):
    """Chat enrichment via any OpenAI-compatible endpoint (NVIDIA NIM, Groq,
    Together, Ollama…). Reuses all the prompts/parsers — only the call differs."""
    def __init__(self, model: str, api_key: str | None, base_url: str | None):
        from openai import OpenAI  # lazy, optional
        self.model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)

    def _call(self, prompt: str, max_tokens: int) -> str:
        msg = self._client.chat.completions.create(
            model=self.model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.choices[0].message.content or ""


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
            tier=_norm_tier(data.get("tier"), content),
            domain=_norm_domain(data.get("domain"), content),
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
                tier=_norm_tier(d.get("tier"), str(d.get("summary", ""))),
                domain=_norm_domain(d.get("domain"), str(d.get("summary", ""))),
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


# Altitude hints for the offline stand-in. The real model judges this far better.
_TIER_HINTS = {
    "core": ("mission", "vision", "philosophy", "our goal", "we believe", "strategy",
             "north star", "the problem we", "company", "market", "long-term", "principle"),
    "mid": ("roadmap", "quarter", "milestone", "plan", "decided to", "we'll build",
            "architecture", "feature", "release", "design", "approach"),
}


def _guess_tier(text: str) -> str:
    low = text.lower()
    for tier, hints in _TIER_HINTS.items():
        if any(h in low for h in hints):
            return tier
    return "leaf"  # default: granular/operational


def _norm_tier(value, content: str) -> str:
    v = str(value or "").strip().lower()
    return v if v in TIERS else _guess_tier(content)


_DOMAIN_HINTS = {
    "technical": ("architecture", "code", "api", "database", "server", "infra", "schema",
                  "pipeline", "deploy", "bug", "performance", "library", "framework"),
    "user": ("user", "customer", "feedback", "behaviour", "behavior", "persona", "churn",
             "onboarding", "retention", "ux", "usability"),
    "market": ("market", "go-to-market", "gtm", "positioning", "pricing", "growth", "sales", "demand"),
    "competitor": ("competitor", "competition", "rival", "alternative", "vs ", "compared to"),
    "legal": ("legal", "compliance", "gdpr", "license", "contract", "policy", "regulation", "privacy"),
    "process": ("process", "workflow", "meeting", "sprint", "standup", "ops", "team", "hiring", "roadmap"),
    "product": ("product", "feature", "mission", "vision", "roadmap", "scope", "use case"),
}


def _guess_domain(text: str) -> str:
    low = text.lower()
    for dom, hints in _DOMAIN_HINTS.items():
        if any(h in low for h in hints):
            return dom
    return "other"


def _norm_domain(value, content: str) -> str:
    v = str(value or "").strip().lower()
    return v if v in DOMAINS else _guess_domain(content)


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
        try:
            return AnthropicProcessor(cfg.processing_model, cfg.anthropic_api_key)
        except Exception as e:  # missing package / bad key — never break the tool
            import sys
            print(f"[crux] Anthropic unavailable ({e}); using offline enrichment. "
                  f"Install with: pip install 'crux[anthropic]'", file=sys.stderr)
    elif cfg.processing_provider in ("openai", "openai_compat", "nvidia"):
        try:
            return OpenAICompatProcessor(cfg.processing_model, cfg.openai_api_key,
                                         getattr(cfg, "api_base", None))
        except Exception as e:
            import sys
            hint = " — run: pip install -U openai" if "proxies" in str(e) else \
                   " — install with: pip install 'crux[openai]'"
            print(f"[crux] OpenAI-compatible processor unavailable ({e}); using offline "
                  f"enrichment{hint}", file=sys.stderr)
    return FakeProcessor()
