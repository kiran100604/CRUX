"""Core data types."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

# The `type` enum is the primary "intent" signal — it drives retrieval ranking.
# decision/constraint/architecture are high-value locked truth; reference/context
# are supporting material; exploration is "thinking out loud" and ranks lowest so
# throwaway spike notes don't leak into unrelated sessions.
ITEM_TYPES = (
    "decision",
    "constraint",
    "architecture",
    "design",
    "reference",
    "context",
    "exploration",
)

HIGH_VALUE_TYPES = frozenset({"decision", "constraint", "architecture"})
LOW_VALUE_TYPES = frozenset({"reference", "context", "exploration"})

# Two-tier memory:
#   individual = working layer, captured as you work — transient, possibly
#                unverified, fades with age. The agent reads it as "what I'm
#                currently doing".
#   main       = the curated graph — only verified truth, promoted explicitly
#                after refinement. Prioritized in retrieval; never decays.
SCOPES = ("individual", "main")

# Altitude / tier — how high-level a fact is, INDEPENDENT of its type. A
# "decision" can be Core ("focus on the India market") or Leaf ("use tabs").
# Classified by the LLM at enrichment time (heuristic only as an offline stand-in).
#   core = company mission, vision, philosophy, the core problem & strategy
#   mid  = product decisions, roadmap, planning, architecture choices
#   leaf = granular operational facts, tasks, references, day-to-day work
TIERS = ("core", "mid", "leaf")
TIER_LABELS = {"core": "Core Strategy", "mid": "Mid Planning", "leaf": "Leaf / Operational"}

# Domain — what a fact is ABOUT (independent of type and tier). Lets the graph
# connect and weight knowledge across areas, and the dashboard filter by it.
# Auto-classified by the LLM at capture (heuristic stand-in offline).
DOMAINS = ("product", "technical", "user", "market", "competitor", "legal", "process", "other")
DOMAIN_LABELS = {
    "product": "Product", "technical": "Technical", "user": "Users",
    "market": "Market", "competitor": "Competitors", "legal": "Legal",
    "process": "Process", "other": "Other",
}

# Everything captured — a note, a clipboard grab, a whole document, an agent's
# observation — is stored first as an Episode: the raw, untouched source of truth
# with provenance. Facts (ContextItems) are extracted FROM episodes and link back.
# This is the foundation that scales from solo notes to company document ingestion.
SOURCE_TYPES = ("note", "file", "paste", "hotkey", "agent")

# A card (a dumped step in a thread) has a KIND — what it IS — decided by the
# router at dump time, never filed by hand. Kept short on purpose: there's nothing
# for the user to learn, and "note" is the catch-all when nothing else fits.
#
# The first group are SIGNALS — the high-value entries that drive working memory
# (decisions/requirements/constraints/conclusions/open questions). The rest are
# supporting material. The router classifies every dump, from any source.
CARD_KINDS = (
    "decision", "requirement", "constraint", "insight", "question",  # signals
    "reference", "prompt", "suggestion", "result", "note",           # supporting
)
# Signals carry the durable "what's decided / what matters" — weighted highest when
# synthesizing working memory and when promoting learnings to the knowledge base.
SIGNAL_KINDS = frozenset({"decision", "requirement", "constraint", "insight", "question"})
CARD_KIND_LABELS = {
    "decision": "Decision", "requirement": "Requirement", "constraint": "Constraint",
    "insight": "Conclusion", "question": "Open question",
    "reference": "Reference", "prompt": "Prompt", "suggestion": "Suggestion",
    "result": "Result", "note": "Note",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Episode:
    id: str
    raw_content: str               # the full original input, never chunked/edited
    source_type: str               # note | file | paste | hotkey | agent | ...
    source_ref: str | None = None  # filename, URL, or app name
    title: str | None = None       # document title, if any
    added_by: str | None = None    # nullable now; identity for company/multi-user later
    thread_id: str | None = None   # the work thread this capture is a step of (if any)
    kind: str = "note"             # router-assigned: reference|prompt|insight|… (what it is)
    approach_id: str | None = None # which approach (direction) it belongs to, if any
    is_guide: bool = False         # a thread-level reference that governs the whole thread
    routed: bool = False           # has the router classified it yet? (else "sorting…")
    route_reason: str | None = None  # short why, for transparency
    included: bool = True          # does this card feed the thread's living context?
    created_at: str = field(default_factory=now_iso)

    def to_public_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_row(row) -> "Episode":
        keys = row.keys()
        g = lambda k, d=None: (row[k] if k in keys else d)
        return Episode(
            id=row["id"], raw_content=row["raw_content"], source_type=row["source_type"],
            source_ref=row["source_ref"], title=row["title"], added_by=row["added_by"],
            thread_id=g("thread_id"),
            kind=g("kind") or "note", approach_id=g("approach_id"),
            is_guide=bool(g("is_guide", 0)), routed=bool(g("routed", 0)),
            route_reason=g("route_reason"), included=bool(g("included", 1)),
            created_at=row["created_at"],
        )


@dataclass
class ContextItem:
    id: str
    raw_content: str
    title: str
    summary: str
    type: str
    tier: str = "leaf"  # core | mid | leaf — altitude, set by enrichment
    domain: str = "other"  # product | technical | user | market | … — what it's about
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    scope: str = "individual"  # everything starts in the working layer
    owner: str | None = None   # who captured it (private working memory is per-owner)
    proposed: bool = True       # individual + proposed → the leader's Review; else private WM
    confidence: float = 0.7  # working capture is provisional; promotion raises it
    superseded_by: str | None = None
    archived: bool = False
    embedding_model: str | None = None
    content_hash: str = ""
    version: int = 1
    promoted_at: str | None = None
    source_episode_id: str | None = None  # which episode this fact came from
    locator: str | None = None            # where in the episode (e.g. section heading)
    captured_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_public_dict(self) -> dict:
        """Serializable view for API/CLI/MCP (no raw embedding bytes)."""
        return asdict(self)

    @staticmethod
    def from_row(row) -> "ContextItem":
        return ContextItem(
            id=row["id"],
            raw_content=row["raw_content"],
            title=row["title"],
            summary=row["summary"],
            type=row["type"],
            tier=(row["tier"] if "tier" in row.keys() else None) or "leaf",
            domain=(row["domain"] if "domain" in row.keys() else None) or "other",
            tags=json.loads(row["tags"] or "[]"),
            source=row["source"],
            scope=row["scope"],
            owner=(row["owner"] if "owner" in row.keys() else None),
            proposed=bool(row["proposed"]) if "proposed" in row.keys() and row["proposed"] is not None else True,
            confidence=row["confidence"],
            superseded_by=row["superseded_by"],
            archived=bool(row["archived"]),
            embedding_model=row["embedding_model"],
            content_hash=row["content_hash"],
            version=row["version"],
            promoted_at=row["promoted_at"],
            source_episode_id=row["source_episode_id"],
            locator=row["locator"],
            captured_at=row["captured_at"],
            updated_at=row["updated_at"],
        )
