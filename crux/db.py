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
import threading
from pathlib import Path

from .embeddings import pack, unpack
from .models import ContextItem, Episode

SCHEMA = """
-- raw sources of truth; facts (items) are extracted from these and link back.
-- An episode is also a "step" in a work thread when thread_id is set — the
-- narrative unit of working memory (captured as-is, never atomized into facts).
CREATE TABLE IF NOT EXISTS episodes (
    id           TEXT PRIMARY KEY,
    raw_content  TEXT NOT NULL,
    source_type  TEXT NOT NULL,
    source_ref   TEXT,
    title        TEXT,
    added_by     TEXT,
    thread_id    TEXT,
    kind         TEXT NOT NULL DEFAULT 'note',  -- router-assigned: what the card IS
    approach_id  TEXT,                           -- which approach (direction) it's in
    is_guide     INTEGER NOT NULL DEFAULT 0,     -- thread-level governing reference
    routed       INTEGER NOT NULL DEFAULT 0,     -- has the router filed it yet?
    route_reason TEXT,
    included     INTEGER NOT NULL DEFAULT 1,     -- does this card feed the thread context?
    created_at   TEXT NOT NULL
);

-- threads: a unit of active work ("what I'm doing now"). Seeded with background
-- context at creation, then steps (episodes) accumulate and a living summary is
-- auto-maintained until the user hand-edits it (summary_owned). The portable
-- brief = background + summary, paste-able into any tool.
CREATE TABLE IF NOT EXISTS threads (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    intent        TEXT,
    background    TEXT,                          -- seeded from the KB at creation
    summary       TEXT,                          -- living auto-summary of the steps
    summary_owned INTEGER NOT NULL DEFAULT 0,    -- 1 once the user edits it by hand
    summary_stale INTEGER NOT NULL DEFAULT 0,    -- 1 when new steps need re-summarizing
    status        TEXT NOT NULL DEFAULT 'active', -- active | done
    current_approach_id TEXT,                      -- the direction being worked now
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- approaches: the different directions tried within a thread (like git branches,
-- but the router creates them — the user never has to). Each keeps its own living
-- summary. status: active (a live direction) | parked (tried, kept for history).
CREATE TABLE IF NOT EXISTS approaches (
    id            TEXT PRIMARY KEY,
    thread_id     TEXT NOT NULL,
    title         TEXT NOT NULL,
    summary       TEXT,
    summary_owned INTEGER NOT NULL DEFAULT 0,
    summary_stale INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approaches_thread ON approaches(thread_id);

-- sessions: a time-bounded work period within a thread. Captures from any source
-- (hotkey, agent hooks, paste) land in the OPEN session; after an idle gap the
-- session auto-closes with a checkpoint summary and the next capture opens a new
-- one. The thread keeps the rolling working memory; sessions give resume points.
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    thread_id   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',   -- active | closed
    summary     TEXT,                             -- checkpoint synthesized at close
    started_at  TEXT NOT NULL,
    last_at     TEXT NOT NULL,                    -- last activity, for idle detection
    ended_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_thread ON sessions(thread_id);

-- tiny key/value for app-level pointers (e.g. the current thread for hotkey capture)
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS items (
    id              TEXT PRIMARY KEY,
    raw_content     TEXT NOT NULL,
    title           TEXT NOT NULL,
    summary         TEXT NOT NULL,
    type            TEXT NOT NULL,
    tier            TEXT NOT NULL DEFAULT 'leaf',
    domain          TEXT NOT NULL DEFAULT 'other',
    subject         TEXT NOT NULL DEFAULT '',
    tags            TEXT NOT NULL DEFAULT '[]',
    source          TEXT,
    scope           TEXT NOT NULL DEFAULT 'individual',
    owner           TEXT,
    proposed        INTEGER NOT NULL DEFAULT 1,
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
    user      TEXT,
    used_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usages_item ON usages(item_id);

-- activity timeline: one human-readable row every time CRUX PULLS context into an
-- agent or CAPTURES something into a project. Written deterministically (by the
-- hook), so the trail is always complete regardless of how the agent renders it —
-- each row deep-links to the exact project page.
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,          -- pull | capture
    thread_id  TEXT,
    session_id TEXT,
    title      TEXT NOT NULL,
    detail     TEXT,
    count      INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);

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

-- graph edges between facts. 'extends' = B adds detail to A (both stay valid).
-- ('updates' stays on items.superseded_by; 'derives' is reserved for later.)
CREATE TABLE IF NOT EXISTS relations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id    TEXT NOT NULL,   -- the new/extending fact
    to_id      TEXT NOT NULL,   -- the existing fact it enriches
    kind       TEXT NOT NULL DEFAULT 'extends',
    reason     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relations_from ON relations(from_id);
CREATE INDEX IF NOT EXISTS idx_relations_to ON relations(to_id);

-- lenses: named views that gather a relevant slice of the graph (emergent
-- "topics"). No fact is filed into a lens — the lens pulls what's relevant.
CREATE TABLE IF NOT EXISTS lenses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    intent     TEXT,
    created_at TEXT NOT NULL
);
"""

# columns a caller may update via `update()` — whitelist guards against injection
_UPDATABLE = {"title", "summary", "type", "tier", "domain", "subject", "tags", "source", "scope",
              "owner", "proposed", "confidence", "superseded_by", "archived",
              "promoted_at", "version"}


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(path)
        # One SQLite connection PER THREAD. uvicorn runs sync endpoints in a
        # threadpool, and a single shared connection raises "bad parameter / API
        # misuse" when two threads use it concurrently. Thread-local connections
        # + WAL give safe concurrent reads and a serialized writer instead.
        self._local = threading.local()
        conn = self._connect()              # main-thread connection
        self._local.conn = conn
        conn.executescript(SCHEMA)
        self._migrate()
        conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL + busy timeout: concurrent readers + one writer across connections,
        # without spurious "database locked".
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
        return c

    def _migrate(self) -> None:
        """Add columns introduced after a db was first created (local, in-place)."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(items)")}
        for col in ("source_episode_id", "locator"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT")
        if "tier" not in cols:
            self.conn.execute("ALTER TABLE items ADD COLUMN tier TEXT NOT NULL DEFAULT 'leaf'")
        if "domain" not in cols:
            self.conn.execute("ALTER TABLE items ADD COLUMN domain TEXT NOT NULL DEFAULT 'other'")
        if "subject" not in cols:
            self.conn.execute("ALTER TABLE items ADD COLUMN subject TEXT NOT NULL DEFAULT ''")
        if "owner" not in cols:
            self.conn.execute("ALTER TABLE items ADD COLUMN owner TEXT")
        if "proposed" not in cols:
            self.conn.execute("ALTER TABLE items ADD COLUMN proposed INTEGER NOT NULL DEFAULT 1")
        ucols = {r["name"] for r in self.conn.execute("PRAGMA table_info(usages)")}
        if "user" not in ucols:
            self.conn.execute("ALTER TABLE usages ADD COLUMN user TEXT")
        ecols = {r["name"] for r in self.conn.execute("PRAGMA table_info(episodes)")}
        if "thread_id" not in ecols:
            self.conn.execute("ALTER TABLE episodes ADD COLUMN thread_id TEXT")
        if "kind" not in ecols:
            self.conn.execute("ALTER TABLE episodes ADD COLUMN kind TEXT NOT NULL DEFAULT 'note'")
        if "approach_id" not in ecols:
            self.conn.execute("ALTER TABLE episodes ADD COLUMN approach_id TEXT")
        if "is_guide" not in ecols:
            self.conn.execute("ALTER TABLE episodes ADD COLUMN is_guide INTEGER NOT NULL DEFAULT 0")
        if "routed" not in ecols:
            self.conn.execute("ALTER TABLE episodes ADD COLUMN routed INTEGER NOT NULL DEFAULT 0")
        if "route_reason" not in ecols:
            self.conn.execute("ALTER TABLE episodes ADD COLUMN route_reason TEXT")
        if "included" not in ecols:
            self.conn.execute("ALTER TABLE episodes ADD COLUMN included INTEGER NOT NULL DEFAULT 1")
        if "session_id" not in ecols:
            self.conn.execute("ALTER TABLE episodes ADD COLUMN session_id TEXT")
        # index lives here, not in SCHEMA: on an upgraded db the column only exists
        # after the ALTER above (SCHEMA runs before _migrate).
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_episodes_thread ON episodes(thread_id)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_episodes_approach ON episodes(approach_id)")
        tcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(threads)")}
        if tcols and "current_approach_id" not in tcols:
            self.conn.execute("ALTER TABLE threads ADD COLUMN current_approach_id TEXT")

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    # --- writes --------------------------------------------------------------

    def insert_episode(self, ep: Episode) -> Episode:
        self.conn.execute(
            """INSERT INTO episodes (id, raw_content, source_type, source_ref, title,
                   added_by, thread_id, session_id, kind, approach_id, is_guide, routed,
                   route_reason, included, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ep.id, ep.raw_content, ep.source_type, ep.source_ref, ep.title,
             ep.added_by, ep.thread_id, ep.session_id, ep.kind, ep.approach_id,
             int(ep.is_guide), int(ep.routed), ep.route_reason, int(ep.included),
             ep.created_at),
        )
        self.conn.commit()
        return ep

    def update_card(self, card_id: str, fields: dict) -> bool:
        """Update a card's (episode's) state: kind / routed / included / approach_id."""
        allowed = {"kind", "approach_id", "is_guide", "routed", "route_reason",
                   "thread_id", "included"}
        cols = {k: (int(v) if k in ("is_guide", "routed", "included") and v is not None else v)
                for k, v in fields.items() if k in allowed}
        if not cols:
            return False
        assignment = ", ".join(f"{c}=?" for c in cols)
        cur = self.conn.execute(
            f"UPDATE episodes SET {assignment} WHERE id=?", (*cols.values(), card_id))
        self.conn.commit()
        return cur.rowcount > 0

    def delete_episode(self, ep_id: str) -> None:
        self.conn.execute("DELETE FROM episodes WHERE id=?", (ep_id,))
        self.conn.commit()

    # --- approaches (directions within a thread) ----------------------------

    def insert_approach(self, a: dict) -> dict:
        self.conn.execute(
            """INSERT INTO approaches (id, thread_id, title, summary, summary_owned,
                   summary_stale, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (a["id"], a["thread_id"], a["title"], a.get("summary"),
             int(a.get("summary_owned", 0)), int(a.get("summary_stale", 0)),
             a.get("status", "active"), a["created_at"], a["updated_at"]),
        )
        self.conn.commit()
        return a

    def get_approach(self, approach_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM approaches WHERE id=?", (approach_id,)).fetchone()
        return dict(row) if row else None

    def list_approaches(self, thread_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM approaches WHERE thread_id=? ORDER BY created_at ASC", (thread_id,))
        return [dict(r) for r in rows]

    def update_approach(self, approach_id: str, fields: dict, updated_at: str) -> bool:
        allowed = {"title", "summary", "summary_owned", "summary_stale", "status"}
        cols = {k: v for k, v in fields.items() if k in allowed}
        cols["updated_at"] = updated_at
        assignment = ", ".join(f"{c}=?" for c in cols)
        cur = self.conn.execute(
            f"UPDATE approaches SET {assignment} WHERE id=?", (*cols.values(), approach_id))
        self.conn.commit()
        return cur.rowcount > 0

    def delete_approach(self, approach_id: str) -> None:
        # cards survive; they fall back to Unsorted
        self.conn.execute("UPDATE episodes SET approach_id=NULL WHERE approach_id=?", (approach_id,))
        self.conn.execute("DELETE FROM approaches WHERE id=?", (approach_id,))
        self.conn.commit()

    def approach_cards(self, approach_id: str) -> list[Episode]:
        rows = self.conn.execute(
            "SELECT * FROM episodes WHERE approach_id=? ORDER BY created_at ASC", (approach_id,))
        return [Episode.from_row(r) for r in rows]

    def thread_guides(self, thread_id: str) -> list[Episode]:
        rows = self.conn.execute(
            "SELECT * FROM episodes WHERE thread_id=? AND is_guide=1 ORDER BY created_at ASC",
            (thread_id,))
        return [Episode.from_row(r) for r in rows]

    def thread_unsorted_cards(self, thread_id: str) -> list[Episode]:
        """Cards in a thread with no home yet: either the router is still working
        (routed=0) or it wasn't confident enough to file them (approach_id NULL)."""
        rows = self.conn.execute(
            """SELECT * FROM episodes WHERE thread_id=? AND is_guide=0 AND approach_id IS NULL
               ORDER BY created_at DESC""", (thread_id,))
        return [Episode.from_row(r) for r in rows]

    def get_episode(self, ep_id: str) -> Episode | None:
        row = self.conn.execute("SELECT * FROM episodes WHERE id=?", (ep_id,)).fetchone()
        return Episode.from_row(row) if row else None

    # --- threads (active work) + meta ---------------------------------------

    def insert_thread(self, t: dict) -> dict:
        self.conn.execute(
            """INSERT INTO threads (id, title, intent, background, summary,
                   summary_owned, summary_stale, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (t["id"], t["title"], t.get("intent"), t.get("background"), t.get("summary"),
             int(t.get("summary_owned", 0)), int(t.get("summary_stale", 0)),
             t.get("status", "active"), t["created_at"], t["updated_at"]),
        )
        self.conn.commit()
        return t

    def get_thread(self, thread_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        return dict(row) if row else None

    def list_threads(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM threads WHERE status=? ORDER BY updated_at DESC", (status,))
        else:
            rows = self.conn.execute("SELECT * FROM threads ORDER BY updated_at DESC")
        return [dict(r) for r in rows]

    def update_thread(self, thread_id: str, fields: dict, updated_at: str) -> bool:
        allowed = {"title", "intent", "background", "summary",
                   "summary_owned", "summary_stale", "status", "current_approach_id"}
        cols = {k: v for k, v in fields.items() if k in allowed}
        cols["updated_at"] = updated_at
        assignment = ", ".join(f"{c}=?" for c in cols)
        cur = self.conn.execute(
            f"UPDATE threads SET {assignment} WHERE id=?", (*cols.values(), thread_id))
        self.conn.commit()
        return cur.rowcount > 0

    def delete_thread(self, thread_id: str) -> None:
        # steps (episodes) survive; just detach them from the thread + its sessions
        self.conn.execute(
            "UPDATE episodes SET thread_id=NULL, session_id=NULL WHERE thread_id=?", (thread_id,))
        self.conn.execute("DELETE FROM sessions WHERE thread_id=?", (thread_id,))
        self.conn.execute("DELETE FROM threads WHERE id=?", (thread_id,))
        self.conn.commit()

    def thread_steps(self, thread_id: str) -> list[Episode]:
        rows = self.conn.execute(
            "SELECT * FROM episodes WHERE thread_id=? ORDER BY created_at ASC", (thread_id,))
        return [Episode.from_row(r) for r in rows]

    # --- sessions (work periods within a thread) -----------------------------

    def insert_session(self, s: dict) -> dict:
        self.conn.execute(
            """INSERT INTO sessions (id, thread_id, status, summary, started_at, last_at, ended_at)
               VALUES (?,?,?,?,?,?,?)""",
            (s["id"], s["thread_id"], s.get("status", "active"), s.get("summary"),
             s["started_at"], s["last_at"], s.get("ended_at")))
        self.conn.commit()
        return s

    def get_session(self, session_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def active_session(self, thread_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE thread_id=? AND status='active' "
            "ORDER BY started_at DESC LIMIT 1", (thread_id,)).fetchone()
        return dict(row) if row else None

    def list_sessions(self, thread_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE thread_id=? ORDER BY started_at ASC", (thread_id,))
        return [dict(r) for r in rows]

    def update_session(self, session_id: str, fields: dict) -> bool:
        allowed = {"status", "summary", "last_at", "ended_at"}
        cols = {k: v for k, v in fields.items() if k in allowed}
        if not cols:
            return False
        assignment = ", ".join(f"{c}=?" for c in cols)
        cur = self.conn.execute(
            f"UPDATE sessions SET {assignment} WHERE id=?", (*cols.values(), session_id))
        self.conn.commit()
        return cur.rowcount > 0

    def session_steps(self, session_id: str) -> list[Episode]:
        rows = self.conn.execute(
            "SELECT * FROM episodes WHERE session_id=? ORDER BY created_at ASC", (session_id,))
        return [Episode.from_row(r) for r in rows]

    def unsorted_steps(self, limit: int = 100) -> list[Episode]:
        """Captured steps not yet attached to any thread (an inbox of stray notes).
        Excludes episodes that were turned into facts (those carry a thread_id only
        when captured as a step), so this is purely human working-memory captures."""
        rows = self.conn.execute(
            """SELECT e.* FROM episodes e
               WHERE e.thread_id IS NULL
                 AND e.source_type IN ('hotkey','popup','note','paste')
                 AND NOT EXISTS (SELECT 1 FROM items i WHERE i.source_episode_id = e.id)
               ORDER BY e.created_at DESC LIMIT ?""", (limit,))
        return [Episode.from_row(r) for r in rows]

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self.conn.execute("DELETE FROM meta WHERE key=?", (key,))
        else:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.conn.commit()

    def insert(self, item: ContextItem, embedding: list[float]) -> ContextItem:
        self.conn.execute(
            """INSERT INTO items (id, raw_content, title, summary, type, tier, domain, subject, tags, source,
                   scope, owner, proposed, confidence, superseded_by, archived, embedding, embedding_model,
                   content_hash, version, promoted_at, source_episode_id, locator,
                   captured_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item.id, item.raw_content, item.title, item.summary, item.type, item.tier,
             item.domain, item.subject, json.dumps(item.tags), item.source, item.scope, item.owner,
             int(item.proposed), item.confidence,
             item.superseded_by, int(item.archived), pack(embedding), item.embedding_model,
             item.content_hash, item.version, item.promoted_at, item.source_episode_id,
             item.locator, item.captured_at, item.updated_at),
        )
        self.conn.commit()
        return item

    def set_embedding(self, item_id: str, vec: list[float], model: str, updated_at: str) -> None:
        self.conn.execute(
            "UPDATE items SET embedding=?, embedding_model=?, updated_at=? WHERE id=?",
            (pack(vec), model, updated_at, item_id))
        self.conn.commit()

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

    def log_usage(self, item_id: str, query: str, session: str, used_at: str,
                  user: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO usages (item_id, query, session, user, used_at) VALUES (?,?,?,?,?)",
            (item_id, query, session, user, used_at),
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

    # --- activity events (pull / capture timeline) ---------------------------

    def log_event(self, kind: str, thread_id: str | None, session_id: str | None,
                  title: str, detail: str, count: int, created_at: str) -> None:
        self.conn.execute(
            """INSERT INTO events (kind, thread_id, session_id, title, detail, count, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (kind, thread_id, session_id, title, detail, int(count), created_at))
        self.conn.commit()

    def recent_events(self, limit: int = 50, thread_id: str | None = None) -> list[dict]:
        if thread_id:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE thread_id=? ORDER BY id DESC LIMIT ?",
                (thread_id, limit))
        else:
            rows = self.conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
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

    # --- graph edges (extends) -----------------------------------------------

    def add_relation(self, from_id: str, to_id: str, kind: str, reason: str,
                     created_at: str) -> None:
        dup = self.conn.execute(
            "SELECT 1 FROM relations WHERE from_id=? AND to_id=? AND kind=?",
            (from_id, to_id, kind)).fetchone()
        if dup:
            return
        self.conn.execute(
            "INSERT INTO relations (from_id, to_id, kind, reason, created_at) VALUES (?,?,?,?,?)",
            (from_id, to_id, kind, reason, created_at))
        self.conn.commit()

    def relations_for(self, item_id: str) -> list[dict]:
        """All edges touching this item, in either direction."""
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM relations WHERE from_id=? OR to_id=?", (item_id, item_id))]

    def delete_relations_for(self, item_id: str) -> None:
        self.conn.execute("DELETE FROM relations WHERE from_id=? OR to_id=?", (item_id, item_id))
        self.conn.commit()

    # --- lenses (emergent topics) --------------------------------------------

    def add_lens(self, name: str, intent: str, created_at: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO lenses (name, intent, created_at) VALUES (?,?,?)",
            (name, intent, created_at))
        self.conn.commit()
        return cur.lastrowid

    def list_lenses(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM lenses ORDER BY id DESC")]

    def get_lens(self, lens_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM lenses WHERE id=?", (lens_id,)).fetchone()
        return dict(row) if row else None

    def delete_lens(self, lens_id: int) -> None:
        self.conn.execute("DELETE FROM lenses WHERE id=?", (lens_id,))
        self.conn.commit()


def _fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 OR-query of bare terms."""
    terms = [t for t in "".join(c if c.isalnum() else " " for c in query).split() if len(t) > 1]
    return " OR ".join(terms)
