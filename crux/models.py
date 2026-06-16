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

# Everything captured — a note, a clipboard grab, a whole document, an agent's
# observation — is stored first as an Episode: the raw, untouched source of truth
# with provenance. Facts (ContextItems) are extracted FROM episodes and link back.
# This is the foundation that scales from solo notes to company document ingestion.
SOURCE_TYPES = ("note", "file", "paste", "hotkey", "agent")


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
    created_at: str = field(default_factory=now_iso)

    def to_public_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_row(row) -> "Episode":
        return Episode(
            id=row["id"], raw_content=row["raw_content"], source_type=row["source_type"],
            source_ref=row["source_ref"], title=row["title"], added_by=row["added_by"],
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
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    scope: str = "individual"  # everything starts in the working layer
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
            tags=json.loads(row["tags"] or "[]"),
            source=row["source"],
            scope=row["scope"],
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
