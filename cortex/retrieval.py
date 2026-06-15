"""Hybrid retrieval — the part that decides whether CORTEX is useful.

Vector cosine + FTS5 lexical, fused with Reciprocal Rank Fusion, then re-weighted
by intent (type), pin state, recency, and superseded status. Pure vector search
surfaces semantically-near noise; this is the standard fix and it's testable
(see eval/).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from .db import Database
from .embeddings import cosine
from .models import ContextItem, HIGH_VALUE_TYPES, LOW_VALUE_TYPES

RRF_K = 60          # standard RRF constant
CANDIDATES = 50     # depth of each list before fusion
DECAY_HALF_LIFE_DAYS = 45.0  # low-value items lose half their recency weight here


@dataclass
class Result:
    item: ContextItem
    score: float


def search(db: Database, query_vec: list[float], query_text: str, limit: int = 5,
           include_archived: bool = False) -> list[Result]:
    # 1. semantic candidates
    sims = sorted(
        ((cosine(query_vec, vec), iid) for iid, vec in db.all_embeddings(include_archived) if vec),
        reverse=True,
    )[:CANDIDATES]
    semantic_ids = [iid for _, iid in sims]

    # 2. lexical candidates
    lexical_ids = db.fts_search(query_text, CANDIDATES, include_archived)

    # 3. RRF fuse
    fused: dict[str, float] = {}
    for rank, iid in enumerate(semantic_ids):
        fused[iid] = fused.get(iid, 0.0) + 1.0 / (RRF_K + rank)
    for rank, iid in enumerate(lexical_ids):
        fused[iid] = fused.get(iid, 0.0) + 1.0 / (RRF_K + rank)

    # 4. boosts / penalties by intent + freshness + trust
    results: list[Result] = []
    for iid, base in fused.items():
        item = db.get(iid)
        if item is None:
            continue
        results.append(Result(item=item, score=base * _weight(item)))

    results.sort(key=lambda r: r.score, reverse=True)
    return results[:limit]


def _weight(item: ContextItem) -> float:
    w = 1.0
    if item.pinned:
        w *= 1.5
    if item.type in HIGH_VALUE_TYPES:
        w *= 1.3
    elif item.type in LOW_VALUE_TYPES:
        w *= 0.9 * _recency_decay(item.captured_at)
    if item.superseded_by:
        w *= 0.15  # demote hard, but never drop — provenance stays queryable
    w *= 0.5 + 0.5 * max(0.0, min(1.0, item.confidence))  # low-trust agent writes rank lower
    return w


def _recency_decay(captured_at: str) -> float:
    try:
        ts = datetime.fromisoformat(captured_at)
    except ValueError:
        return 1.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
    return math.pow(0.5, max(0.0, age_days) / DECAY_HALF_LIFE_DAYS)
