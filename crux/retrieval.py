"""Hybrid retrieval — the part that decides whether CRUX is useful.

Vector cosine + FTS5 lexical, fused with Reciprocal Rank Fusion, then re-weighted
by scope (main = verified truth, individual = working memory that decays), intent
(type), recency, trust (confidence), and superseded status. Pure vector search
surfaces semantically-near noise; this is the standard fix and it's testable
(see eval/).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .db import Database
from .embeddings import cosine
from .models import ContextItem, HIGH_VALUE_TYPES, LOW_VALUE_TYPES

RRF_K = 60          # standard RRF constant
CANDIDATES = 50     # depth of each list before fusion
DECAY_HALF_LIFE_DAYS = 30.0  # working (individual) items halve in weight this often
MAIN_BOOST = 1.5    # verified truth outranks working notes
SUBJECT_BOOST = 1.7  # a fact whose SUBJECT the query names ranks well above off-subject noise


@dataclass
class Result:
    item: ContextItem
    score: float


def search(db: Database, query_vec: list[float], query_text: str, limit: int = 5,
           include_archived: bool = False, scope: str | None = None) -> list[Result]:
    """scope=None searches both tiers (main prioritized); "main"/"individual"
    restricts to one tier."""
    # 1. semantic candidates
    sims = sorted(
        ((cosine(query_vec, vec), iid)
         for iid, vec in db.all_embeddings(include_archived, scope) if vec),
        reverse=True,
    )[:CANDIDATES]
    semantic_ids = [iid for _, iid in sims]

    # 2. lexical candidates
    lexical_ids = db.fts_search(query_text, CANDIDATES, include_archived, scope)

    # 2b. subject candidates — facts whose SUBJECT the query names. A third channel
    # (not just a re-rank boost) so a subject-relevant fact is guaranteed into the
    # pool with real rank, even when the semantic/lexical channels missed it.
    subject_ids = [iid for iid, subj in db.subjects(scope)
                   if _subject_matches(subj, query_text)][:CANDIDATES]

    # 3. RRF fuse the three channels
    fused: dict[str, float] = {}
    for rank, iid in enumerate(semantic_ids):
        fused[iid] = fused.get(iid, 0.0) + 1.0 / (RRF_K + rank)
    for rank, iid in enumerate(lexical_ids):
        fused[iid] = fused.get(iid, 0.0) + 1.0 / (RRF_K + rank)
    for rank, iid in enumerate(subject_ids):
        fused[iid] = fused.get(iid, 0.0) + 1.0 / (RRF_K + rank)

    # 4. boosts / penalties by scope + intent + freshness + trust + subject match
    results: list[Result] = []
    for iid, base in fused.items():
        item = db.get(iid)
        if item is None:
            continue
        results.append(Result(item=item, score=base * _weight(item) * _subject_boost(item, query_text)))

    results.sort(key=lambda r: r.score, reverse=True)
    return results[:limit]


def _stem_match(a: str, b: str) -> bool:
    """Loose token match so 'competition' hits 'competitors', 'storage' hits
    'stored', etc. — share a 4+ char prefix (or be equal when short)."""
    n = min(len(a), len(b))
    if n < 4:
        return a == b
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i >= 4


_SUBJ_STOP = {"the", "and", "for", "with", "that", "this", "into", "about", "what",
              "are", "how", "does", "crux"}  # generic words don't identify a subject


def _subject_matches(subject: str, query: str) -> bool:
    """Does the query name this subject? True on a substring hit, or when any
    SIGNIFICANT subject token stem-matches a query token — so 'competition' hits
    'competitors' and 'working memory and sessions' hits 'sessions', while generic
    words ('crux', 'what') never trigger a match."""
    subj = (subject or "").strip().lower()
    if not subj:
        return False
    q = (query or "").lower()
    if subj in q:
        return True
    sig = [t for t in re.findall(r"[a-z0-9]+", subj) if len(t) >= 4 and t not in _SUBJ_STOP]
    q_toks = re.findall(r"[a-z0-9]+", q)
    return any(_stem_match(st, qt) for st in sig for qt in q_toks)


def _subject_boost(item: ContextItem, query: str) -> float:
    """Re-rank lift on top of the subject channel: an on-subject fact ranks above
    off-subject neighbors (never excludes anything — recall stays intact)."""
    return SUBJECT_BOOST if _subject_matches(item.subject or "", query or "") else 1.0


def _weight(item: ContextItem) -> float:
    w = 1.0
    # scope: main is durable verified truth; individual is working memory that fades
    if item.scope == "main":
        w *= MAIN_BOOST
    else:
        w *= _recency_decay(item.captured_at)
    # intent
    if item.type in HIGH_VALUE_TYPES:
        w *= 1.3
    elif item.type in LOW_VALUE_TYPES:
        w *= 0.9
    if item.superseded_by:
        w *= 0.15  # demote hard, but never drop — provenance stays queryable
    w *= 0.5 + 0.5 * max(0.0, min(1.0, item.confidence))  # low-trust writes rank lower
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
