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

    def create_episode(self, content: str, *, source_type: str = "paste",
                       source_ref: str | None = None, title: str | None = None) -> Episode:
        """Public: persist the raw episode synchronously (so callers get an id),
        then process_episode() can run the heavy extraction — in a worker if async."""
        content = content.strip()
        if not content:
            raise ValueError("cannot ingest empty content")
        return self._episode(content, source_type, source_ref, title)

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
        stored = self.db.insert(item, vec)
        self._detect_conflicts(stored)  # flag contradictions at write time
        return stored

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
        facts = self.process_episode(ep.id, content, source_type=source_type,
                                     source_ref=source_ref, scope=scope, confidence=confidence)
        return {"episode": ep, "facts": facts}

    def process_episode(self, episode_id: str, content: str, *, source_type: str = "file",
                        source_ref: str | None = None, scope: str = "individual",
                        confidence: float = 0.6) -> list[ContextItem]:
        """Chunk an already-created episode's content into facts. Separate from
        ingest() so a background worker can run it on its own DB connection."""
        facts: list[ContextItem] = []
        seen: set[str] = set()
        for locator, piece in chunk(content, source_type):
            for enr in self.processor.extract_facts(piece):
                f = self._store_fact(raw=piece, enr=enr, episode_id=episode_id,
                                     locator=locator, source=source_ref,
                                     scope=scope, confidence=confidence)
                if f.id not in seen:
                    seen.add(f.id); facts.append(f)
        return facts

    def edit(self, item_id: str, *, title: str | None = None, summary: str | None = None,
             type: str | None = None, tags: list | None = None) -> bool:
        """Edit a fact's text. Re-embeds and re-checks contradictions so search
        and conflict detection never go stale on edited facts."""
        full = self.db.resolve_id(item_id)
        if not full:
            return False
        item = self.db.get(full)
        fields: dict = {"version": item.version + 1}
        if title is not None: fields["title"] = title
        if summary is not None: fields["summary"] = summary
        if type is not None: fields["type"] = type
        if tags is not None: fields["tags"] = tags
        self.db.update(full, fields, now_iso())
        updated = self.db.get(full)
        if title is not None or summary is not None:  # text changed → re-embed
            vec = self.embedder.embed(f"{updated.title}\n{updated.summary}\n{updated.raw_content}")
            self.db.set_embedding(full, vec, self.embedder.model, now_iso())
            self._detect_conflicts(self.db.get(full))
        return True

    def ingest_file(self, path: str, *, scope: str = "individual") -> dict:
        p = Path(path)
        text = p.read_text(encoding="utf-8", errors="ignore")
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

    # --- contradiction-aware writes (the "neighborhood update") --------------

    NEIGH_THRESHOLD = 0.80   # only check genuinely close neighbors
    HEURISTIC_FLAG = 0.90    # offline (no LLM judge): flag above this similarity
    MAX_CHECK = 3            # cap judgments per new fact (bounds cost on bulk ingest)

    def _detect_conflicts(self, item: ContextItem) -> None:
        """When a fact lands, scan its neighborhood and flag likely
        contradictions — LLM-judged with a real key, similarity-based offline.
        Never auto-resolves; just records candidates for the human in Review."""
        from .embeddings import cosine
        vec = self.db.embedding_of(item.id)
        if not vec:
            return
        sims = []
        for oid, ovec in self.db.all_embeddings():  # non-archived only
            if oid == item.id or not ovec:
                continue
            s = cosine(vec, ovec)
            if s >= self.NEIGH_THRESHOLD:
                sims.append((s, oid))
        sims.sort(reverse=True)
        for s, oid in sims[: self.MAX_CHECK]:
            other = self.db.get(oid)
            if not other or other.superseded_by:
                continue
            if other.source_episode_id and other.source_episode_id == item.source_episode_id:
                continue  # facts from the same document aren't contradictions
            verdict, reason = self.processor.judge_contradiction(item.summary, other.summary)
            flagged = (s >= self.HEURISTIC_FLAG) if verdict is None else verdict
            if flagged:
                self.db.add_conflict(item.id, oid, round(s, 3),
                                     reason or f"{int(s * 100)}% similar", now_iso())

    def open_conflicts(self, limit: int = 12) -> list[dict]:
        out = []
        for row in self.db.conflict_rows(limit * 3):
            a, b = self.db.get(row["new_id"]), self.db.get(row["existing_id"])
            if (not a or not b or a.archived or b.archived
                    or a.superseded_by or b.superseded_by):
                self.db.set_conflict_status(row["id"], "resolved")  # stale, clean it up
                continue
            out.append({"id": row["id"], "similarity": row["similarity"],
                        "reason": row["reason"], "a": a.to_public_dict(), "b": b.to_public_dict()})
            if len(out) >= limit:
                break
        return out

    def dismiss_conflict(self, conflict_id: int) -> bool:
        self.db.set_conflict_status(int(conflict_id), "dismissed")
        return True

    # --- triage: what needs the human's attention ----------------------------

    def review(self) -> dict:
        """Everything awaiting the human: working items to promote + conflicts."""
        return {"working": self.db.list(scope="individual", archived=False, limit=200),
                "conflicts": self.open_conflicts()}

    # similarity bands for the inbox triage (offline = pure embedding math)
    DUP_SIM = 0.93      # near-identical → likely already captured
    REFINE_SIM = 0.80   # close + same type → likely a newer version of an existing item

    def triage(self) -> list[dict]:
        """Classify each working item against everything you already know, so the
        inbox can show a status dot (clean / attention / conflict) and *why*. The
        clean ones are bulk-promotable; flagged ones force a conscious choice."""
        from .embeddings import cosine

        working = [i for i in self.db.list(scope="individual", archived=False, limit=200)
                   if not i.superseded_by]
        # prefetch all live items once (avoid per-candidate DB hits)
        live = {i.id: i for i in self.db.list(archived=False, limit=100000)
                if not i.superseded_by}

        # which working items are in an open conflict, and against what
        conflict_of: dict[str, dict] = {}
        for c in self.open_conflicts(limit=50):
            for me, other in ((c["a"], c["b"]), (c["b"], c["a"])):
                conflict_of.setdefault(me["id"], {
                    "conflict_id": c["id"], "other": other,
                    "reason": c["reason"], "similarity": c["similarity"]})

        embeds = [(oid, v) for oid, v in self.db.all_embeddings() if v]
        out = []
        for it in working:
            d = it.to_public_dict()
            status, relation, related = "clean", "new", None

            if it.id in conflict_of:
                cf = conflict_of[it.id]
                status, relation = "conflict", "conflict"
                related = dict(cf["other"], similarity=cf["similarity"])
                d["conflict_id"] = cf["conflict_id"]
            else:
                vec = self.db.embedding_of(it.id)
                best = None
                if vec:
                    for oid, ovec in embeds:
                        if oid == it.id:
                            continue
                        other = live.get(oid)
                        if not other:
                            continue
                        if (other.source_episode_id
                                and other.source_episode_id == it.source_episode_id):
                            continue
                        s = cosine(vec, ovec)
                        if best is None or s > best[0]:
                            best = (s, other)
                if best and best[0] >= self.REFINE_SIM:
                    s, other = best
                    related = dict(other.to_public_dict(), similarity=round(s, 3))
                    if s >= self.DUP_SIM:
                        status, relation = "attention", "duplicate"
                    elif other.type == it.type:
                        status, relation = "attention", "refines"
                    else:
                        relation = "relates"  # informational; still clean

            d["status"] = status
            d["fit"] = {"relation": relation, "related": related}
            d["implication"] = self._implication(relation, it, related)
            out.append(d)

        rank = {"conflict": 0, "attention": 1, "clean": 2}
        out.sort(key=lambda d: rank[d["status"]])
        return out

    @staticmethod
    def _implication(relation: str, it, related) -> str:
        def short(s, n=46):
            s = (s or "").strip()
            return s if len(s) <= n else s[:n - 1] + "…"
        pct = (f"{int(related['similarity'] * 100)}%"
               if related and related.get("similarity") else "")
        if relation == "conflict":
            return f"Contradicts '{short(related and related.get('title'))}'. Resolve before promoting."
        if relation == "duplicate":
            return f"Near-duplicate of '{short(related['title'])}' ({pct}). You may already have this."
        if relation == "refines":
            return f"Newer version of '{short(related['title'])}' ({pct} similar)? Consider superseding it."
        if relation == "relates":
            return f"Relates to '{short(related['title'])}'."
        return f"New {it.type or 'note'}. Nothing similar yet."

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
        ok = self.db.update(full, fields, now_iso())
        if ok:
            if title is not None or summary is not None:  # refined text → re-embed
                it = self.db.get(full)
                vec = self.embedder.embed(f"{it.title}\n{it.summary}\n{it.raw_content}")
                self.db.set_embedding(full, vec, self.embedder.model, now_iso())
            self._detect_conflicts(self.db.get(full))  # now verified — recheck vs truth
        return ok

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
        ok = self.db.update(full, {"archived": int(value)}, now_iso())
        if ok and value:
            self.db.resolve_conflicts_for(full)
        return ok

    def supersede(self, old_id: str, new_id: str) -> bool:
        old_full = self.db.resolve_id(old_id)
        new_full = self.db.resolve_id(new_id)
        if not new_full:
            raise ValueError(f"replacement item {new_id} does not exist")
        if not old_full:
            return False
        ok = self.db.update(old_full, {"superseded_by": new_full}, now_iso())
        if ok:  # resolving a conflict clears it from Review
            self.db.resolve_conflicts_for(old_full)
            self.db.resolve_conflicts_for(new_full)
        return ok


def _extract_url(text: str) -> str | None:
    m = re.search(r"https?://\S+", text)
    return m.group(0) if m else None
