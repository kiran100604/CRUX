"""SQLite storage — the single source of truth.

One file. Metadata + raw_content + embedding BLOBs in `items`; an FTS5 mirror in
`items_fts` for lexical search, kept in sync by triggers. No second datastore,
so there is nothing to keep in sync across processes and a backup is one file.

Two-tier memory (individual vs main) is a `scope` column, NOT a second table —
promotion flips the field, so an item is never duplicated and can't drift.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .embeddings import pack, unpack
from .models import ContextItem, Episode

SCHEMA = """
-- raw sources of truth; facts (items) are extracted from these and link back
CREATE TABLE IF NOT EXISTS episodes (
    id           TEXT PRIMARY KEY,
    raw_content  TEXT NOT NULL,
    source_type  TEXT NOT NULL,
    source_ref   TEXT,
    title        TEXT,
    added_by     TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id              TEXT PRIMARY KEY,
    raw_content     TEXT NOT NULL,
    title           TEXT NOT NULL,
    summary         TEXT NOT NULL,
    type            TEXT NOT NULL,
    tags            TEXT NOT NULL DEFAULT '[]',
    source          TEXT,
    scope           TEXT NOT NULL DEFAULT 'individual',
    confidence      REAL NOT NULL DEFAULT 0.7,
    superseded_by   TEXT,
    archived        INTEGER NOT NULL DEFAULT 0,
    embedding       BLOB,
    embedding_model TEXT,
    content_hash    TEXT NOT NULL DEFAULT '',
    version         INTEGER NOT NULL DEFAULT 1,
    promoted_at     TEXT,
    source_episode_id TEXT,
    locator         TEXT,
    captured_at     TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_hash ON items(content_hash);
CREATE INDEX IF NOT EXISTS idx_items_archived ON items(archived);
CREATE INDEX IF NOT EXISTS idx_items_scope ON items(scope);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, summary, tags, raw_content,
    content='items', content_rowid='rowid'
);

-- keep the FTS mirror in sync
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, summary, tags, raw_content)
    VALUES (new.rowid, new.title, new.summary, new.tags, new.raw_content);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, summary, tags, raw_content)
    VALUES ('delete', old.rowid, old.title, old.summary, old.tags, old.raw_content);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, summary, tags, raw_content)
    VALUES ('delete', old.rowid, old.title, old.summary, old.tags, old.raw_content);
    INSERT INTO items_fts(rowid, title, summary, tags, raw_content)
    VALUES (new.rowid, new.title, new.summary, new.tags, new.raw_content);
END;

-- the payoff loop: one row every time an item is injected into a real agent
-- session, so the dashboard can show "used N times" and glow crystals by impact.
CREATE TABLE IF NOT EXISTS usages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id   TEXT NOT NULL,
    query     TEXT,
    session   TEXT,
    used_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usages_item ON usages(item_id);

-- contradiction candidates found at write time (the "neighborhood update").
-- Never auto-applied — the human resolves (supersede) or dismisses; dismissals stick.
CREATE TABLE IF NOT EXISTS conflicts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    new_id      TEXT NOT NULL,
    existing_id TEXT NOT NULL,
    similarity  REAL,
    reason      TEXT,
    status      TEXT NOT NULL DEFAULT 'open',  -- open | resolved | dismissed
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conflicts_status ON conflicts(status);
"""

# columns a caller may update via `update()` — whitelist guards against injection
_UPDATABLE = {"title", "summary", "type", "tags", "source", "scope",
              "confidence", "superseded_by", "archived", "promoted_at"}


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: uvicorn runs sync endpoints in a threadpool;
        # single-user local access makes shared-connection use safe enough here.
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a db was first created (local, in-place)."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(items)")}
        for col in ("source_episode_id", "locator"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT")

    def close(self) -> None:
        self.conn.close()

    # --- writes --------------------------------------------------------------

    def insert_episode(self, ep: Episode) -> Episode:
        self.conn.execute(
            """INSERT INTO episodes (id, raw_content, source_type, source_ref, title,
                   added_by, created_at) VALUES (?,?,?,?,?,?,?)""",
            (ep.id, ep.raw_content, ep.source_type, ep.source_ref, ep.title,
             ep.added_by, ep.created_at),
        )
        self.conn.commit()
        return ep

    def get_episode(self, ep_id: str) -> Episode | None:
        row = self.conn.execute("SELECT * FROM episodes WHERE id=?", (ep_id,)).fetchone()
        return Episode.from_row(row) if row else None

    def insert(self, item: ContextItem, embedding: list[float]) -> ContextItem:
        self.conn.execute(
            """INSERT INTO items (id, raw_content, title, summary, type, tags, source,
                   scope, confidence, superseded_by, archived, embedding, embedding_model,
                   content_hash, version, promoted_at, source_episode_id, locator,
                   captured_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item.id, item.raw_content, item.title, item.summary, item.type,
             json.dumps(item.tags), item.source, item.scope, item.confidence,
             item.superseded_by, int(item.archived), pack(embedding), item.embedding_model,
             item.content_hash, item.version, item.promoted_at, item.source_episode_id,
             item.locator, item.captured_at, item.updated_at),
        )
        self.conn.commit()
        return item

    def update(self, item_id: str, fields: dict, updated_at: str) -> bool:
        """Update a whitelisted set of columns. `tags` may be passed as a list."""
        cols = {}
        for k, v in fields.items():
            if k not in _UPDATABLE:
                raise ValueError(f"unsupported column: {k}")
            cols[k] = json.dumps(v) if k == "tags" else v
        cols["updated_at"] = updated_at
        assignment = ", ".join(f"{c}=?" for c in cols)
        cur = self.conn.execute(
            f"UPDATE items SET {assignment} WHERE id=?", (*cols.values(), item_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    # --- reads ---------------------------------------------------------------

    def get(self, item_id: str) -> ContextItem | None:
        row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return ContextItem.from_row(row) if row else None

    def resolve_id(self, ref: str) -> str | None:
        """Accept a full id or a short prefix (as shown by `list`/`query`).
        Returns the full id only if the prefix is unambiguous."""
        if self.conn.execute("SELECT 1 FROM items WHERE id=?", (ref,)).fetchone():
            return ref
        rows = self.conn.execute(
            "SELECT id FROM items WHERE id LIKE ? LIMIT 2", (ref + "%",)
        ).fetchall()
        return rows[0]["id"] if len(rows) == 1 else None

    def get_by_hash(self, content_hash: str) -> ContextItem | None:
        row = self.conn.execute(
            "SELECT * FROM items WHERE content_hash=? LIMIT 1", (content_hash,)
        ).fetchone()
        return ContextItem.from_row(row) if row else None

    def embedding_of(self, item_id: str) -> list[float]:
        row = self.conn.execute("SELECT embedding FROM items WHERE id=?", (item_id,)).fetchone()
        return unpack(row["embedding"]) if row else []

    def all_embeddings(self, include_archived: bool = False, scope: str | None = None):
        """Yield (id, embedding) for cosine scan. Fine via brute force for solo
        scale; swap to sqlite-vec only if item counts ever make this slow."""
        clauses, params = [], []
        if not include_archived:
            clauses.append("archived=0")
        if scope:
            clauses.append("scope=?"); params.append(scope)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        for row in self.conn.execute(f"SELECT id, embedding FROM items{where}", params):
            yield row["id"], unpack(row["embedding"])

    def fts_search(self, query: str, limit: int, include_archived: bool = False,
                   scope: str | None = None):
        """Return ids ranked by FTS5 bm25 (best first)."""
        match = _fts_query(query)
        if not match:
            return []
        clauses, params = ["items_fts MATCH ?"], [match]
        if not include_archived:
            clauses.append("i.archived=0")
        if scope:
            clauses.append("i.scope=?"); params.append(scope)
        params.append(limit)
        sql = (
            "SELECT i.id FROM items_fts f JOIN items i ON i.rowid=f.rowid "
            f"WHERE {' AND '.join(clauses)} ORDER BY bm25(items_fts) LIMIT ?"
        )
        return [r["id"] for r in self.conn.execute(sql, params)]

    def list(self, type: str | None = None, scope: str | None = None,
             archived: bool = False, limit: int = 100) -> list[ContextItem]:
        clauses, params = ["archived=?"], [int(archived)]
        if type:
            clauses.append("type=?"); params.append(type)
        if scope:
            clauses.append("scope=?"); params.append(scope)
        sql = f"SELECT * FROM items WHERE {' AND '.join(clauses)} ORDER BY captured_at DESC LIMIT ?"
        params.append(limit)
        return [ContextItem.from_row(r) for r in self.conn.execute(sql, params)]

    # --- usage / payoff loop -------------------------------------------------

    def log_usage(self, item_id: str, query: str, session: str, used_at: str) -> None:
        self.conn.execute(
            "INSERT INTO usages (item_id, query, session, used_at) VALUES (?,?,?,?)",
            (item_id, query, session, used_at),
        )
        self.conn.commit()

    def usage_counts(self) -> dict[str, int]:
        return {r["item_id"]: r["n"] for r in self.conn.execute(
            "SELECT item_id, COUNT(*) n FROM usages GROUP BY item_id")}

    def usage_last(self) -> dict[str, str]:
        return {r["item_id"]: r["last"] for r in self.conn.execute(
            "SELECT item_id, MAX(used_at) last FROM usages GROUP BY item_id")}

    def recent_usages(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            """SELECT u.item_id, u.query, u.used_at, i.title, i.scope, i.type
               FROM usages u JOIN items i ON i.id = u.item_id
               ORDER BY u.id DESC LIMIT ?""", (limit,))
        return [dict(r) for r in rows]

    # --- contradiction candidates --------------------------------------------

    def add_conflict(self, new_id: str, existing_id: str, similarity: float,
                     reason: str, created_at: str) -> None:
        dup = self.conn.execute(
            """SELECT 1 FROM conflicts WHERE status='open'
               AND ((new_id=? AND existing_id=?) OR (new_id=? AND existing_id=?))""",
            (new_id, existing_id, existing_id, new_id)).fetchone()
        if dup:
            return
        self.conn.execute(
            """INSERT INTO conflicts (new_id, existing_id, similarity, reason, status, created_at)
               VALUES (?,?,?,?,'open',?)""", (new_id, existing_id, similarity, reason, created_at))
        self.conn.commit()

    def conflict_rows(self, limit: int = 40) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM conflicts WHERE status='open' ORDER BY similarity DESC LIMIT ?", (limit,))]

    def set_conflict_status(self, conflict_id: int, status: str) -> None:
        self.conn.execute("UPDATE conflicts SET status=? WHERE id=?", (status, conflict_id))
        self.conn.commit()

    def resolve_conflicts_for(self, item_id: str) -> None:
        self.conn.execute(
            "UPDATE conflicts SET status='resolved' WHERE status='open' AND (new_id=? OR existing_id=?)",
            (item_id, item_id))
        self.conn.commit()


def _fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 OR-query of bare terms."""
    terms = [t for t in "".join(c if c.isalnum() else " " for c in query).split() if len(t) > 1]
    return " OR ".join(terms)
