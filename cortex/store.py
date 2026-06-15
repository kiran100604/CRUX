"""High-level orchestration shared by every surface (CLI, MCP, hook, HTTP).

This is the only place capture and search logic lives — each entrypoint is a thin
shell over a Store, so behaviour can't drift between, say, the hook and the CLI.
"""

from __future__ import annotations

import hashlib
import re
import uuid

from .config import Config
from .db import Database
from .embeddings import get_embedding_provider
from .models import ContextItem, now_iso
from .processing import get_processor
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

    # --- capture -------------------------------------------------------------

    def capture(self, content: str, *, source: str | None = None,
                type_hint: str | None = None, pinned: bool = False,
                confidence: float = 0.9) -> ContextItem:
        content = content.strip()
        if not content:
            raise ValueError("cannot capture empty content")

        content_hash = _hash(content)
        existing = self.db.get_by_hash(content_hash)
        if existing:
            return existing  # dedup: don't create near-duplicates

        enr = self.processor.enrich(content)
        item = ContextItem(
            id=str(uuid.uuid4()),
            raw_content=content,
            title=enr.title,
            summary=enr.summary,
            type=type_hint or enr.type,
            tags=enr.tags,
            source=source or _extract_url(content),
            pinned=pinned,
            confidence=confidence,
            embedding_model=self.embedder.model,
            content_hash=content_hash,
        )
        vec = self.embedder.embed(f"{item.title}\n{item.summary}\n{content}")
        return self.db.insert(item, vec)

    # --- retrieval -----------------------------------------------------------

    def search(self, query: str, limit: int = 5, include_archived: bool = False) -> list[Result]:
        qvec = self.embedder.embed(query)
        return search(self.db, qvec, query, limit=limit, include_archived=include_archived)

    # --- mutations -----------------------------------------------------------

    def pin(self, item_id: str, value: bool = True) -> bool:
        return self.db.set_flag(item_id, "pinned", int(value), now_iso())

    def archive(self, item_id: str, value: bool = True) -> bool:
        return self.db.set_flag(item_id, "archived", int(value), now_iso())

    def supersede(self, old_id: str, new_id: str) -> bool:
        if not self.db.get(new_id):
            raise ValueError(f"replacement item {new_id} does not exist")
        return self.db.set_flag(old_id, "superseded_by", new_id, now_iso())


def _extract_url(text: str) -> str | None:
    m = re.search(r"https?://\S+", text)
    return m.group(0) if m else None
