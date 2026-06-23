"""Enrichment: raw text -> {title, summary, type, tags}.

Real path uses Anthropic Haiku. The `fake` path is a cheap heuristic so capture
never hard-fails when there's no API key — the loop stays usable offline and the
item can be re-enriched later.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .models import CARD_KINDS, DOMAINS, ITEM_TYPES, TIERS

_TIER_GUIDE = """- tier: one of core | mid | leaf — the ALTITUDE of the fact (independent of type)
    core = company mission, vision, philosophy, the core problem & overall strategy
    mid  = product decisions, roadmap, planning, architecture choices
    leaf = granular operational facts, tasks, references, day-to-day work
- domain: one of product | technical | user | market | competitor | legal | process | other
    — what the fact is ABOUT (technical=architecture/code/infra, user=behaviour/feedback,
    market=positioning/go-to-market, process=team workflow/ops)"""

_PROMPT = """You process captured snippets for a developer/company context store.
Return ONLY minified JSON with keys: title, summary, subject, type, tier, domain, tags.
- title: <= 8 words, no trailing punctuation
- summary: exactly one sentence
- subject: the specific thing this is ABOUT — a service / component / feature / area
    (e.g. "sync service", "auth", "pricing", "retrieval"); <=4 words, lowercase; "" if truly general
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

Return ONLY a minified JSON array; each element has keys: title, summary, subject, type, tier, domain, tags.
- title: <= 8 words, no trailing punctuation
- summary: exactly one sentence
- subject: the specific thing it is ABOUT — a service / component / feature / area
    (e.g. "sync service", "auth", "pricing"); <=4 words, lowercase; "" if truly general
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


_ROUTE_PROMPT = """You organize a working thread for someone doing a task. They
just DUMP things in; your job is to file each one so they never have to. Decide
where this NEW ITEM belongs.

THE TASK (intent): {intent}

EXISTING APPROACHES (directions already being tried):
{approaches}

RECENT ITEMS (most recent last), for continuity:
{recent}

NEW ITEM:
\"\"\"{item}\"\"\"

Return ONLY minified JSON:
{{"kind":"decision|requirement|constraint|insight|question|reference|prompt|suggestion|result|note",
 "target":"guide|current|new|<existing approach id>",
 "new_title":"<=6 words, only when target=new",
 "confidence":0.0-1.0,
 "reason":"<=10 words"}}

Rules:
- target "guide": a reference/spec/principle/PRD meant to steer the WHOLE task,
  not a step inside one direction (e.g. "use these design principles").
- target "current": continues the direction they're working on now. This is the
  DEFAULT — prefer it unless there's a clear reason not to.
- target "<existing approach id>": clearly belongs to one of the approaches listed.
- target "new": ONLY if it's clearly a DIFFERENT approach/idea than any listed.
  Be conservative — do not fragment the thread.
- If you're genuinely unsure where it goes, lower confidence (< 0.45); it will be
  set aside for the user rather than misfiled.
- kind — prefer a SIGNAL when one fits (these drive the working memory):
  decision=a choice made or direction settled (incl. what was ruled out);
  requirement=something that must be true/done; constraint=a limit/rule to respect;
  insight=a conclusion or learning reached; question=an open/unresolved question.
  Otherwise supporting material: reference=a link/doc/spec; prompt=an instruction
  given to an AI; suggestion=an idea not yet acted on; result=an outcome/observation;
  note=anything else."""


_EXTRACT_PROMPT = """You read a chunk of work — a chat transcript, an agent's
output, a block of notes — and pull out the discrete points worth remembering for
an ongoing project's WORKING MEMORY. One entry per distinct point; keep each
self-contained (resolve "it"/"that" so the entry stands alone). Skip greetings,
filler, restated questions, and anything with no lasting signal. If there's
nothing worth keeping, return [].

Return ONLY a minified JSON array; each element:
{{"content":"<the point, one sentence, self-contained>",
  "kind":"decision|requirement|constraint|insight|question|reference|prompt|suggestion|result|note"}}

Prefer SIGNAL kinds when they fit: decision=a choice made / direction settled;
requirement=something that must be true/done; constraint=a limit or rule;
insight=a conclusion or learning; question=an open/unresolved question.

CHUNK:
\"\"\"
{content}
\"\"\""""


def _parse_entries(text: str) -> list[dict]:
    """Normalize the extractor's JSON array into [{content, kind}]. Defensive: bad
    output yields [] so a transcript paste never crashes capture."""
    out: list[dict] = []
    try:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        arr = json.loads(m.group(0) if m else text)
    except Exception:
        return out
    if not isinstance(arr, list):
        return out
    for d in arr:
        if not isinstance(d, dict):
            continue
        content = str(d.get("content", "")).strip()
        if not content:
            continue
        kind = str(d.get("kind", "note")).lower().strip()
        out.append({"content": content, "kind": kind if kind in CARD_KINDS else "note"})
    return out


def _parse_route(text: str, approach_ids: set[str]) -> dict:
    """Normalize the router's JSON into an actionable decision. Defensive: any
    garbage degrades to a low-confidence 'note' so a bad model reply never crashes
    capture (it just lands in Unsorted)."""
    out = {"kind": "note", "target": "current", "new_title": None,
           "confidence": 0.3, "reason": ""}
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        d = json.loads(m.group(0) if m else text)
    except Exception:
        return out
    kind = str(d.get("kind", "note")).lower().strip()
    out["kind"] = kind if kind in CARD_KINDS else "note"
    target = str(d.get("target", "current")).strip()
    if target in ("guide", "current", "new") or target in approach_ids:
        out["target"] = target
    out["new_title"] = (str(d.get("new_title", "")).strip() or None) if target == "new" else None
    try:
        out["confidence"] = max(0.0, min(1.0, float(d.get("confidence", 0.3))))
    except (TypeError, ValueError):
        out["confidence"] = 0.3
    out["reason"] = str(d.get("reason", ""))[:80]
    return out


_CONTEXT_PROMPT = """You maintain the WORKING MEMORY for a piece of work: the
evolving record of what's actually been decided and where things stand, so the
person (or any AI tool) can resume without re-deriving it. The GOAL is tracked
separately as INTENT — do NOT restate or paraphrase it. Working memory is the
*how it's going*, not the *what we're trying to do*.

INTENT (the stable goal — given only for context; do NOT repeat it back):
{intent}

PRIOR WORKING MEMORY (refine this; keep what's still true, drop what's been
superseded):
{prior}

ITEMS dumped so far (oldest first — references, prompts, results, ideas,
learnings, from any tool or agent). Synthesize, don't list:
{items}

Rewrite the WORKING MEMORY now as a tight synthesis of:
- Decisions made and conclusions reached (and what was explicitly ruled out).
- Requirements, constraints, and preferences that have surfaced.
- Current state — what's done, what's in progress right now.
- Open questions still unresolved.
If the latest items show a pivot, follow the new direction and drop the
abandoned one. Be concrete; cite specifics. Do NOT open by restating the goal.
No preamble, no headings like "Working memory:", just the text."""


_ANSWER_PROMPT = """You answer questions about a team's own knowledge base. Use
ONLY the numbered FACTS below — they are the team's verified knowledge. Cite the
facts you rely on inline as [n]. If the facts don't cover the question, say so
plainly ("I don't have anything verified on that") — never invent. Be concise and
direct; synthesize across facts rather than listing them.

CONVERSATION SO FAR:
{history}

FACTS:
{facts}

QUESTION: {question}

Answer (grounded in the facts, with [n] citations):"""


@dataclass
class Enrichment:
    title: str
    summary: str
    type: str
    tags: list[str]
    tier: str = "leaf"
    domain: str = "other"
    subject: str = ""   # the specific thing it's about (scoping key for retrieval)


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
            subject=_guess_subject(clean),
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

    def route(self, item: str, *, intent: str = "", approaches=None, recent=None) -> dict:
        # Offline stand-in so dumping still visibly SORTS without a key. Keyword
        # heuristics for kind; never auto-branches (too unreliable without a model)
        # — everything continues the current approach, the user splits by dragging.
        t = " ".join(item.split()).lower()
        if "?" in t and not re.search(r"https?://", t):
            kind = "question"
        elif re.search(r"\b(decided|going with|let'?s go with|we'?ll use|chose|settled on|drop(ping)?|instead of|no longer|won'?t)\b", t):
            kind = "decision"
        elif re.search(r"\b(must|should|needs? to|has to|require[ds]?|shall)\b", t):
            kind = "requirement"
        elif re.search(r"\b(can'?t|cannot|limit(ed|ation)?|only|at most|budget|deadline|max |within )\b", t):
            kind = "constraint"
        elif re.search(r"https?://|www\.|\.com|\.md\b|\.pdf\b", t):
            kind = "reference"
        elif re.search(r"\b(prompt|generate|create an?|write me|make an?|draw)\b", t):
            kind = "prompt"
        elif re.search(r"\b(learned|realized|takeaway|insight|turns out|lesson|conclude)\b", t):
            kind = "insight"
        elif re.search(r"\b(result|output|came out|too |worked|failed|broke|error)\b", t):
            kind = "result"
        elif re.search(r"\b(suggest|idea|maybe|could|recommend)\b", t):
            kind = "suggestion"
        else:
            kind = "note"
        is_guide = kind == "reference" and bool(
            re.search(r"\b(principle|prd|spec|guideline|according to|brand|style guide)\b", t))
        return {"kind": kind, "target": "guide" if is_guide else "current",
                "new_title": None, "confidence": 0.55, "reason": "offline heuristic"}

    def extract_entries(self, content: str) -> list[dict]:
        # Offline: split a chunk into candidate points and classify each with the
        # same keyword heuristic the router uses. Splits on blank lines, list
        # bullets, and sentence breaks; drops trivially short fragments.
        raw = re.split(r"\n{2,}|\n\s*[-*•]\s+|(?<=[.!?])\s+(?=[A-Z])", content.strip())
        out: list[dict] = []
        for piece in raw:
            p = " ".join(piece.split()).lstrip("-*• ").strip()
            if len(p) < 12:           # too short to be a standalone point
                continue
            out.append({"content": p, "kind": self.route(p)["kind"]})
        return out

    def answer(self, question: str, facts: list, history: list | None = None) -> str:
        # Offline: no model to synthesize prose, so return an honest extractive
        # answer — the top related facts, cited. Goes to real synthesis with a key.
        if not facts:
            return "I don't have anything verified about that yet. Try Browse, or capture it first."
        lines = ["From your verified knowledge:"]
        for n, title, summary, _raw in facts:
            lines.append(f"[{n}] {summary or title}")
        return "\n".join(lines)

    def refine_context(self, intent: str, items: list[str], prior: str = "") -> str:
        # Offline: no model to synthesize, so present an honest working memory =
        # the points kept in scope. The goal lives in INTENT, kept out of here.
        kept = [i.strip().replace("\n", " ") for i in items if i.strip()]
        if not kept:
            return ""
        lines = ["Captured so far:"]
        lines.extend(f"- {i[:200]}" for i in kept[-12:])
        return "\n".join(lines)


class AnthropicProcessor:
    def __init__(self, model: str, api_key: str | None):
        import anthropic  # lazy, optional dependency

        self.model = model
        # Bounded timeout + no retries: a missing/blocked key must fail fast, never
        # hang the app (a misconfigured provider once stalled a request for ~80s).
        self._client = anthropic.Anthropic(api_key=api_key, timeout=20.0, max_retries=0)

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

    def route(self, item: str, *, intent: str = "", approaches=None, recent=None) -> dict:
        """The brain: decide kind + destination (guide / current / an existing
        approach / a new approach) for a freshly dumped item. This is what makes
        'dump and it sorts itself' real."""
        approaches = approaches or []
        recent = recent or []
        ap_text = "\n".join(
            f"- id={a['id']} | {a.get('title','')}: {(a.get('summary') or '').splitlines()[0] if a.get('summary') else ''}"
            for a in approaches) or "(none yet — this is the first direction)"
        rec_text = "\n".join(f"- {r.strip()[:200]}" for r in recent[-5:]) or "(none)"
        try:
            text = self._call(_ROUTE_PROMPT.format(
                intent=intent or "(unspecified)", approaches=ap_text,
                recent=rec_text, item=item[:2000]), 200)
            return _parse_route(text, {a["id"] for a in approaches})
        except Exception:
            return FakeProcessor.route(self, item, intent=intent,
                                       approaches=approaches, recent=recent)

    def refine_context(self, intent: str, items: list[str], prior: str = "") -> str:
        """Re-synthesize the thread's WORKING MEMORY (decisions/state/learnings,
        distinct from the stable intent) from the items still in scope — the heart
        of 'just keep dumping, AI keeps it sharp'."""
        joined = "\n".join(f"- {i.strip()}" for i in items if i.strip())[:8000] or "(nothing yet)"
        try:
            return self._call(_CONTEXT_PROMPT.format(
                intent=intent or "(unspecified)", prior=(prior or "(none yet)")[:3000],
                items=joined), 600).strip()
        except Exception:
            return FakeProcessor.refine_context(self, intent, items, prior)

    def extract_entries(self, content: str) -> list[dict]:
        """Split a chunk (transcript, agent output, notes) into discrete typed
        working-memory entries. Falls back to the offline splitter on any failure."""
        try:
            text = self._call(_EXTRACT_PROMPT.format(content=content[:8000]), 900)
            entries = _parse_entries(text)
            return entries if entries else FakeProcessor.extract_entries(self, content)
        except Exception:
            return FakeProcessor.extract_entries(self, content)

    def answer(self, question: str, facts: list, history: list | None = None) -> str:
        """Grounded Q&A over the knowledge base: synthesize an answer from the
        numbered facts, citing [n]. Multi-turn aware via the conversation history."""
        if not facts:
            return "I don't have anything verified about that yet."
        fact_text = "\n".join(
            f"[{n}] {title}: {summary}\n    {(' '.join((raw or '').split()))[:300]}"
            for n, title, summary, raw in facts)
        hist = "\n".join(f"{'User' if m.get('role')=='user' else 'CRUX'}: {m.get('content','')}"
                         for m in (history or [])[-6:]) or "(start of conversation)"
        try:
            return self._call(_ANSWER_PROMPT.format(
                history=hist, facts=fact_text, question=question[:1000]), 700).strip()
        except Exception:
            return FakeProcessor.answer(self, question, facts, history)


class OpenAICompatProcessor(AnthropicProcessor):
    """Chat enrichment via any OpenAI-compatible endpoint (NVIDIA NIM, Groq,
    Together, Ollama…). Reuses all the prompts/parsers — only the call differs."""
    def __init__(self, model: str, api_key: str | None, base_url: str | None):
        from openai import OpenAI  # lazy, optional
        self.model = model
        # bounded timeout + no retries — fail fast instead of hanging the app
        kw = {"api_key": api_key, "timeout": 20.0, "max_retries": 0}
        if base_url:
            kw["base_url"] = base_url
        self._client = OpenAI(**kw)

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
            subject=_norm_subject(data.get("subject"), content),
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
                subject=_norm_subject(d.get("subject"), str(d.get("summary", ""))),
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


def _guess_subject(text: str) -> str:
    """Offline stand-in for 'what is this about'. The real model does far better;
    here we take the most salient keyword as a coarse subject (empty = general)."""
    tags = _guess_tags(text)
    return tags[0] if tags else ""


def _norm_subject(value, content: str) -> str:
    v = " ".join(str(value or "").split()).strip().lower()[:40]
    return v or _guess_subject(content)


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
