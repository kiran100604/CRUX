"""High-level orchestration shared by every surface (CLI, MCP, hook, HTTP).

This is the only place capture and search logic lives — each entrypoint is a thin
shell over a Store, so behaviour can't drift between, say, the hook and the CLI.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

from .chunk import chunk
from .config import Config
from .db import Database
from .embeddings import get_embedding_provider
from .models import ContextItem, Episode, now_iso
from .processing import Enrichment, get_processor
from .retrieval import Result, search


def _hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).lower().encode()).hexdigest()


class Store:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.ensure_home()
        self.db = Database(cfg.db_path)
        self.embedder = get_embedding_provider(cfg)
        self.processor = get_processor(cfg)

    def close(self) -> None:
        self.db.close()

    # --- ingestion: every input becomes an Episode, then 1..N facts ----------

    def _episode(self, content: str, source_type: str, source_ref: str | None,
                 title: str | None = None) -> Episode:
        return self.db.insert_episode(Episode(
            id=str(uuid.uuid4()), raw_content=content, source_type=source_type,
            source_ref=source_ref, title=title))

    def _store_fact(self, *, raw: str, enr: Enrichment, episode_id: str,
                    locator: str | None, source: str | None, scope: str,
                    confidence: float) -> ContextItem:
        h = _hash(f"{enr.title}\n{enr.summary}")
        existing = self.db.get_by_hash(h)
        if existing:
            return existing  # dedup: same fact, don't duplicate
        item = ContextItem(
            id=str(uuid.uuid4()), raw_content=raw, title=enr.title, summary=enr.summary,
            type=enr.type, tags=enr.tags, source=source, scope=scope, confidence=confidence,
            promoted_at=now_iso() if scope == "main" else None,
            embedding_model=self.embedder.model, content_hash=h,
            source_episode_id=episode_id, locator=locator or None)
        vec = self.embedder.embed(f"{enr.title}\n{enr.summary}\n{raw}")
        return self.db.insert(item, vec)

    def capture(self, content: str, *, source: str | None = None,
                type_hint: str | None = None, scope: str = "individual",
                confidence: float = 0.7, source_type: str = "note") -> ContextItem:
        """Capture a single note → one Episode, one Fact. (Used by quick add,
        hotkey, agent.) For documents that should yield many facts, use ingest()."""
        content = content.strip()
        if not content:
            raise ValueError("cannot capture empty content")
        ep = self._episode(content, source_type, source)
        enr = self.processor.enrich(content)
        if type_hint:
            enr.type = type_hint
        return self._store_fact(raw=content, enr=enr, episode_id=ep.id, locator=None,
                                source=source or _extract_url(content),
                                scope=scope, confidence=confidence)

    def ingest(self, content: str, *, source_type: str = "file",
               source_ref: str | None = None, title: str | None = None,
               scope: str = "individual", confidence: float = 0.6) -> dict:
        """Ingest a document (any text/source): one Episode → chunk → extract
        many facts, each linked back to the episode + its location."""
        content = content.strip()
        if not content:
            raise ValueError("cannot ingest empty content")
        ep = self._episode(content, source_type, source_ref, title)
        facts: list[ContextItem] = []
        seen: set[str] = set()
        for locator, piece in chunk(content, source_type):
            for enr in self.processor.extract_facts(piece):
                f = self._store_fact(raw=piece, enr=enr, episode_id=ep.id,
                                     locator=locator, source=source_ref,
                                     scope=scope, confidence=confidence)
                if f.id not in seen:
                    seen.add(f.id); facts.append(f)
        return {"episode": ep, "facts": facts}

    def ingest_file(self, path: str, *, scope: str = "individual") -> dict:
        p = Path(path)
        text = p.read_text(errors="ignore")
        return self.ingest(text, source_type="file", source_ref=p.name,
                           title=p.stem, scope=scope)

    # --- retrieval -----------------------------------------------------------

    def search(self, query: str, limit: int = 5, include_archived: bool = False,
               scope: str | None = None) -> list[Result]:
        qvec = self.embedder.embed(query)
        return search(self.db, qvec, query, limit=limit,
                      include_archived=include_archived, scope=scope)

    def record_usage(self, item_ids: list[str], query: str, session: str = "") -> None:
        """Log that these items were injected into a live agent session — this is
        what powers the dashboard's 'used N times' payoff loop."""
        ts = now_iso()
        for iid in item_ids:
            self.db.log_usage(iid, query, session, ts)

    # --- triage: what needs the human's attention ----------------------------

    def conflicts(self, threshold: float = 0.82, limit: int = 8) -> list[dict]:
        """Find pairs of verified (main) items that look like they might
        contradict — high semantic similarity, neither already superseded. The
        human resolves by superseding one. (Sharper with real embeddings.)"""
        from .embeddings import cosine
        mains = [i for i in self.db.list(scope="main", archived=False, limit=500)
                 if not i.superseded_by]
        embs = dict(self.db.all_embeddings(scope="main"))
        by_id = {i.id: i for i in mains}
        pairs = []
        ids = list(by_id)
        for a in range(len(ids)):
            for b in range(a + 1, len(ids)):
                va, vb = embs.get(ids[a]), embs.get(ids[b])
                if va and vb:
                    s = cosine(va, vb)
                    if s >= threshold:
                        pairs.append((s, ids[a], ids[b]))
        pairs.sort(reverse=True)
        return [{"similarity": round(s, 3),
                 "a": by_id[x].to_public_dict(), "b": by_id[y].to_public_dict()}
                for s, x, y in pairs[:limit]]

    def review(self) -> dict:
        """Everything awaiting the human: working items to promote + conflicts."""
        return {"working": self.db.list(scope="individual", archived=False, limit=200),
                "conflicts": self.conflicts()}

    # --- promotion: the refinement gate (individual -> main) -----------------

    def promote(self, item_id: str, *, title: str | None = None,
                summary: str | None = None, type: str | None = None,
                confidence: float = 0.95) -> bool:
        """Move a working item into the verified `main` graph, optionally refining
        its fields. This is how only-true things enter the trusted layer."""
        full = self.db.resolve_id(item_id)
        if not full:
            return False
        fields: dict = {"scope": "main", "confidence": confidence,
                        "promoted_at": now_iso()}
        if title is not None:
            fields["title"] = title
        if summary is not None:
            fields["summary"] = summary
        if type is not None:
            fields["type"] = type
        return self.db.update(full, fields, now_iso())

    def demote(self, item_id: str) -> bool:
        """Send a main item back to the working layer (undo a promotion)."""
        full = self.db.resolve_id(item_id)
        if not full:
            return False
        return self.db.update(full, {"scope": "individual", "promoted_at": None}, now_iso())

    # --- other mutations -----------------------------------------------------

    def archive(self, item_id: str, value: bool = True) -> bool:
        full = self.db.resolve_id(item_id)
        if not full:
            return False
        return self.db.update(full, {"archived": int(value)}, now_iso())

    def supersede(self, old_id: str, new_id: str) -> bool:
        old_full = self.db.resolve_id(old_id)
        new_full = self.db.resolve_id(new_id)
        if not new_full:
            raise ValueError(f"replacement item {new_id} does not exist")
        if not old_full:
            return False
        return self.db.update(old_full, {"superseded_by": new_full}, now_iso())


def _extract_url(text: str) -> str | None:
    m = re.search(r"https?://\S+", text)
    return m.group(0) if m else None
