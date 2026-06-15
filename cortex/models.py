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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ContextItem:
    id: str
    raw_content: str
    title: str
    summary: str
    type: str
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    pinned: bool = False
    confidence: float = 0.9  # human capture is trusted; agent writes lower this
    superseded_by: str | None = None
    archived: bool = False
    embedding_model: str | None = None
    content_hash: str = ""
    version: int = 1
    captured_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_public_dict(self) -> dict:
        """Serializable view for API/CLI/MCP (no raw embedding bytes)."""
        d = asdict(self)
        return d

    @staticmethod
    def from_row(row) -> "ContextItem":
        return ContextItem(
            id=row["id"],
            raw_content=row["raw_content"],
            title=row["title"],
            summary=row["summary"],
            type=row["type"],
            tags=json.loads(row["tags"] or "[]"),
            source=row["source"],
            pinned=bool(row["pinned"]),
            confidence=row["confidence"],
            superseded_by=row["superseded_by"],
            archived=bool(row["archived"]),
            embedding_model=row["embedding_model"],
            content_hash=row["content_hash"],
            version=row["version"],
            captured_at=row["captured_at"],
            updated_at=row["updated_at"],
        )
