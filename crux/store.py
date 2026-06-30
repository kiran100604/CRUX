"""High-level orchestration shared by every surface (CLI, MCP, hook, HTTP).

This is the only place capture and search logic lives — each entrypoint is a thin
shell over a Store, so behaviour can't drift between, say, the hook and the CLI.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .chunk import chunk
from .config import Config
from .db import Database
from .embeddings import get_embedding_provider
from .models import CARD_KIND_LABELS, CARD_KINDS, ContextItem, Episode, now_iso
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
        self._last_refine: dict[str, float] = {}  # thread_id → monotonic ts of last refine
        self._degraded: dict[str, bool] = {}      # thread_id → last refine fell back (AI timeout)
        self._qcache: dict[str, list] = {}         # query text → embedding (per provider)

    def close(self) -> None:
        self.db.close()

    def reload_providers(self) -> None:
        """Re-read config.env and rebuild the embedder + processor — so adding a key
        in the setup wizard goes live without restarting the server."""
        self.cfg = Config.load()
        self.embedder = get_embedding_provider(self.cfg)
        self.processor = get_processor(self.cfg)
        self._qcache.clear()   # new provider → old query embeddings are invalid

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

    @staticmethod
    def _descriptor(*, subject: str, type: str, title: str, summary: str) -> str:
        """The text we EMBED for a fact: a clean 'about-ness' descriptor
        (subject · type · meaning) rather than raw prose, so the vector carries the
        point, not filler. Raw text stays searchable via FTS for recall."""
        head = f"{(subject or '').strip()} — " if (subject or "").strip() else ""
        return f"{head}{type}: {title}. {summary}".strip()

    @staticmethod
    def _subject_from_locator(locator: str | None) -> str:
        """A document section heading IS 'what this is about' — use the deepest
        heading as the subject when enrichment didn't name one."""
        if not locator:
            return ""
        seg = locator.split("›")[-1].strip()
        seg = re.sub(r"\s*\(\d+\)$", "", seg)   # drop the "(2)" long-split suffix
        return seg.lower()[:40]

    def _store_fact(self, *, raw: str, enr: Enrichment, episode_id: str,
                    locator: str | None, source: str | None, scope: str,
                    confidence: float, owner: str | None = None,
                    proposed: bool = True) -> ContextItem:
        h = _hash(f"{enr.title}\n{enr.summary}")
        existing = self.db.get_by_hash(h)
        if existing:
            return existing  # dedup: same fact, don't duplicate
        subject = (getattr(enr, "subject", "") or "").strip().lower() \
            or self._subject_from_locator(locator)
        item = ContextItem(
            id=str(uuid.uuid4()), raw_content=raw, title=enr.title, summary=enr.summary,
            type=enr.type, tier=getattr(enr, "tier", "leaf"),
            domain=getattr(enr, "domain", "other"), subject=subject,
            tags=enr.tags, source=source,
            scope=scope, owner=owner, proposed=proposed, confidence=confidence,
            promoted_at=now_iso() if scope == "main" else None,
            embedding_model=self.embedder.model, content_hash=h,
            source_episode_id=episode_id, locator=locator or None)
        # File the fact into the KB tree BEFORE write, so it's stored with its node
        # and contradiction-checking can scope to that node's subtree.
        item.subject_path = self._place_in_tree(item, locator=locator)
        vec = self.embedder.embed(self._descriptor(
            subject=item.subject, type=item.type, title=item.title, summary=item.summary))
        stored = self.db.insert(item, vec)
        self._detect_conflicts(stored)  # flag contradictions at write time
        return stored

    # --- KB tree: where each fact lives, so the KB grows like documentation ---

    def _tree_outline(self, limit: int = 200) -> str:
        """The current taxonomy as `path | description` lines — the context the
        placer reasons over to reuse a node instead of inventing a near-duplicate."""
        lines = []
        for n in self.db.list_nodes()[:limit]:
            desc = (n.get("description") or "").strip()
            lines.append(f"{n['path']} | {desc}" if desc else n["path"])
        return "\n".join(lines)

    def _ensure_node_path(self, path: str, leaf_title: str | None = None,
                          leaf_desc: str = "") -> None:
        """Materialize a path and every ancestor as nodes (idempotent). Intermediate
        segments get a title derived from the slug; the leaf keeps the placer's
        title/description so the deepest node reads well."""
        segs = [s for s in path.split("/") if s]
        ts = now_iso()
        for i, seg in enumerate(segs):
            p = "/".join(segs[: i + 1])
            parent = "/".join(segs[:i]) or None
            is_leaf = i == len(segs) - 1
            title = (leaf_title if is_leaf and leaf_title
                     else seg.replace("-", " ").capitalize())
            self.db.upsert_node(p, title, parent, leaf_desc if is_leaf else "", ts)

    @staticmethod
    def _locator_path(locator: str | None) -> str:
        """A markdown heading trail ("Review › Conflicts") is itself a tree path —
        turn it into a slug path ("review/conflicts") to seed placement with the
        document's real structure."""
        if not locator:
            return ""
        from .processing import _norm_path
        segs = [s for s in re.split(r"›|/", locator) if s.strip()]
        return _norm_path("/".join(s.strip() for s in segs))

    def _place_in_tree(self, item: ContextItem, *, locator: str | None = None) -> str:
        """Ask the processor which node this fact belongs at (reuse or new), then
        ensure that node + ancestors exist. Best-effort: a placement failure must
        never block the write — the fact just lands unplaced (subject_path='')."""
        try:
            res = self.processor.place(
                {"title": item.title, "summary": item.summary,
                 "subject": item.subject, "domain": item.domain, "type": item.type},
                tree=self._tree_outline(), hint=self._locator_path(locator))
        except Exception:
            res = None
        if not res or not res.get("path"):
            return ""
        self._ensure_node_path(res["path"], res.get("title"), res.get("description", ""))
        return res["path"]

    def tree(self) -> list[dict]:
        """The KB taxonomy for display: each node with its direct fact count and a
        recursive total (so a parent shows how much lives beneath it)."""
        nodes = self.db.list_nodes()
        direct = self.db.node_counts()
        total: dict[str, int] = {}
        for n in nodes:                       # roll direct counts up to every ancestor
            segs = n["path"].split("/")
            for i in range(len(segs)):
                total["/".join(segs[: i + 1])] = total.get("/".join(segs[: i + 1]), 0)
        for path, c in direct.items():
            segs = path.split("/")
            for i in range(len(segs)):
                anc = "/".join(segs[: i + 1])
                total[anc] = total.get(anc, 0) + c
        return [{**n, "count": direct.get(n["path"], 0),
                 "total": total.get(n["path"], 0)} for n in nodes]

    def node_facts(self, path: str) -> list[dict]:
        """The facts that live at a node or anywhere beneath it (subtree view)."""
        ids = set(self.db.subtree_item_ids(path))
        return [it.to_public_dict() for it in self.db.list(archived=False, limit=100000)
                if it.id in ids and not it.superseded_by]

    def capture(self, content: str, *, source: str | None = None,
                type_hint: str | None = None, scope: str = "individual",
                confidence: float = 0.7, source_type: str = "note",
                owner: str | None = None, proposed: bool = True) -> ContextItem:
        """Capture a single note → one Episode, one Fact. `proposed=True` sends it
        to the leader's Review; `proposed=False` keeps it as private working memory
        (the owner's scratch — never reviewed, expires)."""
        content = content.strip()
        if not content:
            raise ValueError("cannot capture empty content")
        ep = self._episode(content, source_type, source)
        enr = self.processor.enrich(content)
        if type_hint:
            enr.type = type_hint
        return self._store_fact(raw=content, enr=enr, episode_id=ep.id, locator=None,
                                source=source or _extract_url(content),
                                scope=scope, confidence=confidence,
                                owner=owner, proposed=proposed)

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
             type: str | None = None, tags: list | None = None,
             tier: str | None = None, domain: str | None = None,
             subject: str | None = None, subject_path: str | None = None) -> bool:
        """Edit a fact's text or filing. Re-embeds and re-checks contradictions
        when text changes so search and conflict detection never go stale.
        tier/domain/subject_path are filing-only (no re-embed). subject/title/
        summary/type feed the embedded descriptor, so changing any re-embeds.
        Re-pathing materializes the new node so the tree stays consistent."""
        full = self.db.resolve_id(item_id)
        if not full:
            return False
        item = self.db.get(full)
        fields: dict = {"version": item.version + 1}
        if title is not None: fields["title"] = title
        if summary is not None: fields["summary"] = summary
        if type is not None: fields["type"] = type
        if tags is not None: fields["tags"] = tags
        if tier is not None: fields["tier"] = tier
        if domain is not None: fields["domain"] = domain
        if subject is not None: fields["subject"] = subject.strip().lower()
        if subject_path is not None:
            from .processing import _norm_path
            path = _norm_path(subject_path)
            fields["subject_path"] = path
            if path:
                self._ensure_node_path(path)
        self.db.update(full, fields, now_iso())
        updated = self.db.get(full)
        if any(x is not None for x in (title, summary, type, subject)):  # descriptor changed → re-embed
            vec = self.embedder.embed(self._descriptor(
                subject=updated.subject, type=updated.type,
                title=updated.title, summary=updated.summary))
            self.db.set_embedding(full, vec, self.embedder.model, now_iso())
            self._detect_conflicts(self.db.get(full))
        return True

    def ingest_file(self, path: str, *, scope: str = "individual") -> dict:
        p = Path(path)
        text = p.read_text(encoding="utf-8", errors="ignore")
        return self.ingest(text, source_type="file", source_ref=p.name,
                           title=p.stem, scope=scope)

    # --- retrieval -----------------------------------------------------------

    def export_items(self) -> list[dict]:
        """Serialize the live KB (working + verified, minus superseded/archived) so
        it can be committed to git — the KB then travels with the repo."""
        return [i.to_public_dict() for i in self.db.list(archived=False, limit=100000)
                if not i.superseded_by]

    def import_items(self, items: list[dict]) -> int:
        """Load exported items into this store, deduped by content hash (idempotent
        — re-importing the same file adds nothing)."""
        n = 0
        for d in items:
            h = d.get("content_hash") or _hash(f"{d.get('title')}\n{d.get('summary')}")
            if self.db.get_by_hash(h):
                continue
            path = d.get("subject_path", "")
            it = ContextItem(
                id=d.get("id") or str(uuid.uuid4()),
                raw_content=d.get("raw_content", ""),
                title=d.get("title", "Untitled"),
                summary=d.get("summary", ""),
                type=d.get("type", "context"),
                tier=d.get("tier", "leaf"),
                domain=d.get("domain", "other"),
                subject=d.get("subject", ""),
                subject_path=path,
                tags=d.get("tags", []),
                source=d.get("source"),
                scope=d.get("scope", "individual"),
                owner=d.get("owner"),
                proposed=d.get("proposed", True),
                confidence=d.get("confidence", 0.7),
                embedding_model=self.embedder.model,
                content_hash=h,
                version=d.get("version", 1),
                promoted_at=d.get("promoted_at"),
                locator=d.get("locator"),
                captured_at=d.get("captured_at") or now_iso(),
                updated_at=now_iso(),
            )
            if path:                            # rebuild the tree as facts land
                self._ensure_node_path(path)
            self.db.insert(it, self.embedder.embed(self._descriptor(
                subject=it.subject, type=it.type, title=it.title, summary=it.summary)))
            n += 1
        return n

    def backfill_tree(self, *, root: str = "") -> int:
        """Place every still-unfiled fact into the KB tree (and materialize its
        nodes). Run after upgrading a pre-tree DB, or after switching to a real
        provider so the placement improves. Returns how many got filed."""
        n = 0
        for it in self.db.list(archived=False, limit=100000):
            if it.subject_path or it.superseded_by:
                continue
            path = self._place_in_tree(it, locator=it.locator)
            if not path and root:               # last resort so nothing stays orphaned
                path = self._slug_root_path(it, root)
                if path:
                    self._ensure_node_path(path)
            if path:
                self.db.update(it.id, {"subject_path": path}, now_iso())
                n += 1
        return n

    @staticmethod
    def _slug_root_path(it: ContextItem, root: str) -> str:
        from .processing import slug
        seg = slug(it.subject) or slug(it.domain if it.domain != "other" else "") or "general"
        return f"{slug(root)}/{seg}"

    def nominate(self, item_id: str) -> bool:
        """Push a private working-memory item into Review (proposed=True)."""
        full = self.db.resolve_id(item_id)
        if not full:
            return False
        return self.db.update(full, {"proposed": 1}, now_iso())

    def working_memory(self, owner: str | None = None) -> list[dict]:
        """A user's private working memory: individual + not proposed + not archived."""
        out = []
        for i in self.db.list(scope="individual", archived=False, limit=1000):
            if i.proposed or i.superseded_by:
                continue
            if owner and i.owner not in (owner, None):
                continue
            out.append(i.to_public_dict())
        return out

    def expire_working_memory(self, days: int = 7) -> int:
        """Archive private working memory older than `days` (never nominations or
        verified facts) — keeps the backlog from piling up; nothing is deleted."""
        from datetime import datetime, timezone
        n = 0
        for i in self.db.list(scope="individual", archived=False, limit=100000):
            if i.proposed or i.superseded_by:   # nominations wait for the leader
                continue
            try:
                ts = datetime.fromisoformat(i.captured_at)
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - ts).days >= days:
                self.archive(i.id)
                n += 1
        return n

    # --- threads: "what I'm doing now" = an intent + a living context --------
    #
    # A thread is a piece of work (an INTENT). You just DUMP things in (prompts,
    # links, results, snippets, ideas). CRUX maintains ONE living CONTEXT — a
    # refined statement of the direction, so any AI instantly knows what you want.
    # You never organize: every included dump refines the context. The only control
    # is whether a card is IN the context — exclude one and the context forgets it.

    def _kb_connections(self, intent: str, memory: str = "") -> str:
        """The verified-KB facts RELEVANT to the current work — retrieved live
        against intent + working memory, and RELEVANCE-GATED so an unrelated KB
        shows nothing (no more 'CRUX background on a trading thread'). Recomputed
        whenever the working memory refreshes, so it tracks what you're doing."""
        q = (intent + "\n" + memory).strip()
        if not q:
            return ""
        from .hooks import _format  # lazy: hooks imports nothing heavy from store
        results, links = self.retrieve(q, limit=6, scope="main")
        rel = [r for r in results if r.relevant]
        if not rel:
            return ""
        keep = {r.item.id for r in rel}
        return _format(rel, [(src, it) for src, it in links if src in keep])

    def create_thread(self, title: str, intent: str = "", *, seed: bool = True) -> dict:
        title = (title or "").strip() or (intent.strip()[:60] or "Untitled thread")
        ts = now_iso()
        # `summary` holds the living CONTEXT (the refined vision); summary_owned/stale
        # track whether the user has taken the pen / whether it needs re-refining.
        t = {"id": str(uuid.uuid4()), "title": title, "intent": intent.strip(),
             "background": self._kb_connections(intent) if seed else "",
             "summary": "", "summary_owned": 0, "summary_stale": 0,
             "status": "active", "created_at": ts, "updated_at": ts}
        self.db.insert_thread(t)
        self.db.set_meta("current_thread", t["id"])  # newest thread becomes current
        return self.db.get_thread(t["id"])

    # --- sessions: auto work-periods within a thread, for continuity ----------
    #
    # Captures from any source land in the thread's OPEN session. After an idle
    # gap the session auto-closes with a checkpoint summary, and the next capture
    # opens a fresh one — so when you come back you get a clean resume point
    # without ever pressing start/stop.

    IDLE_GAP_SECONDS = 2 * 3600  # a gap longer than this starts a new session

    @staticmethod
    def _age_seconds(iso: str | None) -> float:
        if not iso:
            return float("inf")
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds()
        except ValueError:
            return float("inf")

    def _active_session(self, thread_id: str) -> str:
        """The id of the thread's open session — bumped if recent, rolled over (old
        one closed with a checkpoint, new one started) once it's been idle too long."""
        ts = now_iso()
        s = self.db.active_session(thread_id)
        if s and self._age_seconds(s["last_at"]) <= self.IDLE_GAP_SECONDS:
            self.db.update_session(s["id"], {"last_at": ts})
            return s["id"]
        if s:                                   # idle too long → checkpoint & roll over
            self._close_session(s["id"])
        new = {"id": str(uuid.uuid4()), "thread_id": thread_id, "status": "active",
               "summary": None, "started_at": ts, "last_at": ts, "ended_at": None}
        self.db.insert_session(new)
        return new["id"]

    def _close_session(self, session_id: str) -> None:
        """Write a checkpoint summary for a session from its in-scope cards, then
        mark it closed. Best-effort: a summary failure still closes the session."""
        s = self.db.get_session(session_id)
        if not s or s["status"] == "closed":
            return
        t = self.db.get_thread(s["thread_id"])
        items = [self._labelled_item(c) for c in self.db.session_steps(session_id)
                 if c.included]
        summary = ""
        if items:
            try:
                summary = self.processor.refine_context(
                    (t["intent"] if t else "") or "", items, "")
            except Exception:
                summary = ""
        self.db.update_session(
            session_id, {"status": "closed", "summary": summary, "ended_at": now_iso()})

    def close_session_now(self, thread_id: str) -> None:
        """Explicitly close the thread's open session (e.g. on finish)."""
        s = self.db.active_session(thread_id)
        if s:
            self._close_session(s["id"])

    def add_step(self, content: str, *, source: str = "note",
                 source_ref: str | None = None,
                 thread_id: str | None = None, route: bool = False,
                 kind: str | None = None, role: str | None = None,
                 _log: bool = True) -> dict:
        """Dump a card. Lands on the given thread (or the current one); with no
        thread it's a global unsorted capture. Kept raw — never atomized into facts.
        `source` is the channel (note/hotkey/agent/…); `source_ref` is the
        provenance label (which tool/agent it came from), shown on the card and
        used to attribute decisions in working memory.
        `kind` lets the user TAG the dump at capture (e.g. flag competitor info as
        'reference' so it's treated as context to be aware of, never folded into our
        own decisions). A valid tag is authoritative — the card is born classified
        and the router is skipped (one fewer LLM call). Untagged dumps arrive
        UNCLASSIFIED ('sorting…') and route_card tags them; route=True classifies
        inline (CLI/offline), the server does it in the background so capture stays
        instant."""
        content = content.strip()
        if not content:
            raise ValueError("cannot capture empty step")
        if thread_id is None:
            thread_id = self.current_thread_id()
        if thread_id and not self.db.get_thread(thread_id):
            thread_id = None
        session_id = self._active_session(thread_id) if thread_id else None
        tagged = kind in CARD_KINDS                # user-supplied tag we trust
        # `role` is the user-facing word they picked (Prompt/Info/Idea/Progress/
        # Decision); we map it to `kind` for treatment but remember the role so the
        # card displays it. Stored as a "role:" sentinel in route_reason.
        reason = (f"role:{role}" if tagged and role else
                  ("tagged at capture" if tagged else None))
        ep = self.db.insert_episode(Episode(
            id=str(uuid.uuid4()), raw_content=content, source_type=source,
            source_ref=(source_ref or None), thread_id=thread_id,
            session_id=session_id, included=True,
            kind=(kind if tagged else "note"),
            routed=tagged, route_reason=reason))
        if thread_id:
            self.db.update_thread(thread_id, {}, now_iso())  # bump updated_at
            if tagged:
                self._mark_context_stale(thread_id)   # born classified → react now
            elif route:
                self.route_card(ep.id)
            if _log:
                self._log_capture(thread_id, source, source_ref, [ep.id],
                                  [kind] if tagged else None)
        return {"episode": self.db.get_episode(ep.id).to_public_dict(),
                "thread_id": thread_id, "card_id": ep.id, "tagged": tagged}

    def resolve_thread(self, ref: str | None, *, create: bool = False) -> str | None:
        """Map a project reference (thread id, or exact active title) to a thread id.
        Falls back to the current thread when ref is empty. With create=True, an
        unmatched non-empty ref starts a new thread by that name — the drop-in path
        for agent hooks that name a project CRUX hasn't seen yet."""
        ref = (ref or "").strip()
        if not ref:
            return self.current_thread_id()
        if self.db.get_thread(ref):
            return ref
        for t in self.db.list_threads(status="active"):
            if t["title"].strip().lower() == ref.lower():
                return t["id"]
        if create:
            return self.create_thread(ref, ref, seed=False)["id"]
        return self.current_thread_id()

    # Auto-capture (the Stop hook) keeps only entries that carry real project
    # signal — decisions, requirements, constraints, conclusions, open questions,
    # results. Chatty "notes" (acknowledgements, status lines) are dropped so an
    # agent's conversational prose can't flood working memory.
    _AUTO_KEEP = frozenset({"decision", "requirement", "constraint", "insight",
                            "question", "result"})

    @staticmethod
    def _is_crux_echo(text: str) -> bool:
        """True for CRUX's own output fed back through the transcript — its
        breadcrumbs and injected context blocks — so it never re-captures itself."""
        t = (text or "").lower()
        return ("⌁ crux" in t or "[crux team context" in t
                or "[working memory" in t or "[intent —" in t
                or "additionalcontext" in t)

    def ingest_working(self, content: str, *, thread_id: str | None = None,
                       source: str = "agent", source_ref: str | None = None,
                       split: bool = True, signals_only: bool = False) -> dict:
        """Bridge a chunk of work (a transcript, an agent's output, a block of
        notes) into a project's working memory. With split=True the chunk is broken
        into discrete, pre-classified typed entries (one card each); otherwise it
        lands as a single dump that the router classifies. signals_only=True (the
        auto-capture path) keeps only signal-bearing entries and drops CRUX's own
        echoed output — so conversational chatter never pollutes the context. All
        entries share the same provenance so working memory can attribute them."""
        content = (content or "").strip()
        if not content:
            raise ValueError("cannot ingest empty content")
        if thread_id is None:
            thread_id = self.current_thread_id()
        if thread_id and not self.db.get_thread(thread_id):
            thread_id = None
        if signals_only and self._is_crux_echo(content):
            return {"thread_id": thread_id, "card_ids": [], "count": 0}
        if not split:
            res = self.add_step(content, source=source, source_ref=source_ref,
                                thread_id=thread_id, _log=False)
            if res.get("card_id"):
                self.route_card(res["card_id"])
            self._log_capture(thread_id, source, source_ref, [res.get("card_id")])
            return {"thread_id": thread_id, "card_ids": [res.get("card_id")], "count": 1}
        entries = self.processor.extract_entries(content) or [
            {"content": content, "kind": "note"}]
        if signals_only:
            entries = [e for e in entries
                       if e.get("kind") in self._AUTO_KEEP
                       and not self._is_crux_echo(e.get("content", ""))]
        if not entries:
            return {"thread_id": thread_id, "card_ids": [], "count": 0}
        session_id = self._active_session(thread_id) if thread_id else None
        card_ids, kinds = [], []
        for e in entries:
            ep = self.db.insert_episode(Episode(
                id=str(uuid.uuid4()), raw_content=e["content"], source_type=source,
                source_ref=(source_ref or None), thread_id=thread_id,
                session_id=session_id, kind=e.get("kind", "note"), routed=True,
                route_reason="extracted", included=True))
            card_ids.append(ep.id); kinds.append(e.get("kind", "note"))
        if thread_id:
            self.db.update_thread(thread_id, {}, now_iso())
            self._mark_context_stale(thread_id)
        self._log_capture(thread_id, source, source_ref, card_ids, kinds)
        return {"thread_id": thread_id, "card_ids": card_ids, "count": len(card_ids)}

    # Signatures of assistant conversational prose (vs a real project signal) — used
    # only to clean up agent AUTO-captures filed before the signals-only filter.
    _CHATTER = ("```", "git pull", "pip install", "standing by", "no action needed",
                "ambient crux", "ready when you", "ready for your", "i'll wait",
                "i'll stay quiet", "let me know", "ps c:\\", "http://localhost",
                "127.0.0.1", "tests green", "committed and pushed", "| tag |", "→",
                "let me ", "i'll ", "want me to", "to see it", "e.g.", "i agree",
                "you're right", "good catch", "pushed (", "i can ", "next step")

    @classmethod
    def _looks_like_chatter(cls, text: str) -> bool:
        # only applied to AGENT auto-captures, so markdown/code fences/these phrases
        # are strong signals of assistant prose rather than a real project signal.
        t = (text or "").lower()
        return (cls._is_crux_echo(text) or "**" in text or "`" in text
                or any(s in t for s in cls._CHATTER))

    # Sources whose captures are MANUAL/curated and must be kept by cleanup. Anything
    # else that's agent-sourced (source_type="agent") is auto-captured prose — the
    # Stop hook / MCP / older hook versions — i.e. the noise.
    _KEEP_SOURCES = frozenset({"debug-agent"})

    def purge_chatter(self, thread_id: str) -> tuple[int, dict]:
        """Remove auto-captured agent prose from a thread's working memory: any
        card whose source_type is 'agent' (the Stop hook / MCP / old hook versions),
        EXCEPT curated sources (the demo seed), plus any CRUX echoes. Manual captures
        (dumps/popup/hotkey) are always kept. Returns (removed, breakdown) where
        breakdown counts cards by source_type for diagnostics. Forces a full rebuild."""
        from collections import Counter
        before = Counter()
        removed = 0
        for c in self.db.thread_steps(thread_id):
            before[c.source_type or "?"] += 1
            auto_agent = c.source_type == "agent" and c.source_ref not in self._KEEP_SOURCES
            if auto_agent or self._is_crux_echo(c.raw_content):
                self.db.delete_episode(c.id)
                removed += 1
        if removed:
            self._mark_context_stale(thread_id, rebuild=True)
        return removed, dict(before)

    def _log_capture(self, thread_id, source, source_ref, card_ids, kinds=None):
        """One activity row for a capture, summarizing what was filed + where from."""
        n = len([c for c in card_ids if c])
        if not thread_id or not n:
            return
        from collections import Counter
        via = source_ref or source or "capture"
        if kinds:
            mix = ", ".join(f"{c}× {k}" for k, c in Counter(kinds).most_common())
            detail = f"{mix} · via {via}"
        else:
            detail = f"{n} item{'s' if n != 1 else ''} · via {via}"
        sid = self.db.active_session(thread_id)
        self.log_event("capture", thread_id=thread_id,
                       session_id=(sid or {}).get("id"),
                       title=f"Captured {n} signal{'s' if n != 1 else ''}",
                       detail=detail, count=n)

    def route_pending(self, thread_id: str) -> int:
        """Classify ALL still-unrouted cards in a thread in ONE LLM call (instead of
        one per card), then mark the context stale once. The background path uses
        this so a burst of dumps costs a single routing call."""
        cards = [c for c in self.db.thread_steps(thread_id) if not c.routed]
        if not cards:
            return 0
        t = self.db.get_thread(thread_id)
        decisions = self.processor.route_many(
            [c.raw_content for c in cards], intent=(t["intent"] if t else "") or "")
        for c, d in zip(cards, decisions):
            self.db.update_card(c.id, {"routed": 1, "kind": d.get("kind", "note"),
                                       "route_reason": d.get("reason")})
        self._mark_context_stale(thread_id)
        return len(cards)

    def route_card(self, card_id: str) -> dict | None:
        """Tag a dumped card's KIND (reference/prompt/result/…) and mark the thread's
        context stale so it re-refines. No lanes to file into — the dump just feeds
        the one living context."""
        ep = self.db.get_episode(card_id)
        if not ep or not ep.thread_id:
            return None
        t = self.db.get_thread(ep.thread_id)
        d = self.processor.route(ep.raw_content, intent=(t["intent"] if t else "") or "",
                                 approaches=[], recent=[])
        self.db.update_card(card_id, {"routed": 1, "kind": d["kind"],
                                      "route_reason": d.get("reason")})
        self._mark_context_stale(ep.thread_id)
        return d

    MIN_REFRESH_INTERVAL = 4.0   # rate-limit refines: 1st action refines now, rapid
                                 # follow-ups coalesce until this interval elapses

    def _mark_context_stale(self, thread_id: str, *, rebuild: bool = False) -> None:
        # Mark on EVERY mutation (dump, delete, include-toggle, intent edit). Bumps
        # updated_at, which the debounce uses as the "last activity" clock. Stale is
        # set even when the user owns the summary — so KB connections still refresh.
        # rebuild=True forces a FULL re-synthesis next refresh: a delete/exclude/
        # intent change can invalidate prior conclusions, so we can't just fold a new
        # dump on top — the whole memory is re-derived. A plain new dump (rebuild
        # False) updates INCREMENTALLY: prior memory + just the new card(s).
        if not self.db.get_thread(thread_id):
            return
        fields = {"summary_stale": 1}
        if rebuild:
            fields["summary_rebuild"] = 1
        self.db.update_thread(thread_id, fields, now_iso())

    @staticmethod
    def _labelled_item(c: Episode) -> str:
        """Prefix a card with its kind and provenance so the synthesis can weight
        signals and attribute decisions to the agent/tool they came from."""
        tags = []
        if c.routed and c.kind:
            tags.append(CARD_KIND_LABELS.get(c.kind, c.kind).lower())
        if c.source_ref:
            tags.append(f"via {c.source_ref}")
        elif c.source_type and c.source_type not in ("note", "paste"):
            tags.append(f"via {c.source_type}")
        prefix = f"[{' · '.join(tags)}] " if tags else ""
        return f"{prefix}{c.raw_content}"

    def ensure_context(self, thread_id: str, *, force: bool = False,
                       refine_llm: bool = True) -> dict | None:
        """Re-derive the living context when it's stale. RATE-LIMITED: the first
        action refines right away; rapid follow-ups (or the 2s poll) coalesce until
        MIN_REFRESH_INTERVAL passes, so a burst of dumps/deletes costs one refine,
        not five. A manual ↻ passes force. On a refresh we re-refine the working
        memory (unless the user owns it) AND recompute the relevance-gated KB
        connections — one consistent reaction per change.
        refine_llm=False is the FAST PATH (page loads, hook injection): it does NO
        model/embedding calls — returns the stored summary as-is — so the UI never
        blocks on a slow LLM. The server schedules the real refresh in the
        background; the next poll picks it up."""
        import time
        t = self.db.get_thread(thread_id)
        if not t:
            return None
        if not (t["summary_stale"] or not t["summary"]):
            return t
        if not refine_llm:
            return t                        # fast path: stored summary, no API calls
        last = self._last_refine.get(thread_id, 0.0)
        if not force and (time.monotonic() - last) < self.MIN_REFRESH_INTERVAL:
            return t                       # rate-limited; a later poll will catch it
        fields: dict = {"summary_stale": 0, "summary_rebuild": 0}
        included = [c for c in self.db.thread_steps(thread_id) if c.included]
        prior = t["summary"] or ""
        # FULL rebuild when a delete/exclude/intent change invalidated prior memory
        # (or there's none yet); otherwise INCREMENTAL — fold only the new, unfolded
        # cards into the existing memory. The incremental call gets the prior working
        # memory + just the new dump(s) with their [tags], and reasons about how they
        # update what we already knew (cheaper, and continuity-preserving).
        rebuild = bool(t["summary_rebuild"]) or not prior
        did_refine = False
        folded_ids: list[str] = []
        if not t["summary_owned"]:
            if rebuild:
                items = [self._labelled_item(c) for c in included]
                fields["summary"] = self.processor.refine_context(
                    t["intent"] or "", items, "") if items else ""
                did_refine = bool(items)   # only a real synthesis counts as an LLM call
            else:
                fresh = [c for c in included if not c.in_summary]
                if fresh:
                    items = [self._labelled_item(c) for c in fresh]
                    fields["summary"] = self.processor.refine_context(
                        t["intent"] or "", items, prior, incremental=True)
                    did_refine = True
                    folded_ids = [c.id for c in fresh]
        memory = fields.get("summary", prior) or ""
        fields["background"] = self._kb_connections(t["intent"] or "", memory)
        self.db.update_thread(thread_id, fields, now_iso())
        if not t["summary_owned"]:          # keep the fold-tracking in step with memory
            if rebuild:
                self.db.resync_in_summary(thread_id)  # in_summary mirrors included
            elif folded_ids:
                self.db.fold_episodes(folded_ids)
        if did_refine:                     # rate-limit the EXPENSIVE part only
            self._last_refine[thread_id] = time.monotonic()
            # remember whether the model actually answered (vs timed out → offline)
            self._degraded[thread_id] = bool(getattr(self.processor, "last_degraded", False))
        return self.db.get_thread(thread_id)

    def thread_view(self, thread_id: str, *, refine_llm: bool = True) -> dict | None:
        # refine_llm=False is the server's FAST PATH: return the stored summary
        # instantly (no model/embedder call) and let the server refresh in the
        # background. CLI/tests/hooks use the default (synchronous) refine.
        t = self.ensure_context(thread_id, refine_llm=refine_llm)
        if not t:
            return None
        cards = [c.to_public_dict() for c in reversed(self.db.thread_steps(thread_id))]
        sessions = self.db.list_sessions(thread_id)
        active = next((s for s in sessions if s["status"] == "active"), None)
        # Resuming = the open session has been idle a while, or it's all closed —
        # surface the last checkpoint so you reload context instantly on return.
        last_closed = next((s for s in reversed(sessions) if s["status"] == "closed"), None)
        resuming = bool(last_closed) and (
            active is None or self._age_seconds(active["last_at"]) > self.IDLE_GAP_SECONDS)
        # One honest WORKING-MEMORY status the UI can show. "sorting" = cards still
        # being classified; "updating" = working memory needs a re-synthesis; else
        # "ready" (settled). NOTE: must NOT be called "status" — the thread record
        # already has `status` (active/done); clobbering it broke active-detection.
        sorting = any(not c["routed"] for c in cards)
        updating = (not t["summary_owned"]) and bool(t["summary_stale"])
        wm_status = "sorting" if sorting else ("updating" if updating else "ready")
        return {**t, "context": t.get("summary") or "",
                "context_owned": bool(t["summary_owned"]),
                "wm_status": wm_status, "updated_at": t["updated_at"],
                "ai_degraded": self._degraded.get(thread_id, False),
                "cards": cards, "card_count": len(cards),
                "sessions": list(reversed(sessions)),  # newest first for display
                "session_count": len(sessions),
                "resume": (last_closed.get("summary") or "") if resuming else "",
                "open_questions": [c["raw_content"] for c in cards
                                   if c.get("kind") == "question" and c.get("included")][:6]}

    def list_threads(self, status: str | None = None) -> list[dict]:
        cur = self.current_thread_id()
        out = []
        for t in self.db.list_threads(status=status):
            steps = self.db.thread_steps(t["id"])
            out.append({**t, "card_count": len(steps),
                        "is_current": t["id"] == cur,
                        "last_step": steps[-1].created_at if steps else t["created_at"]})
        return out

    def edit_thread(self, thread_id: str, *, title: str | None = None,
                    intent: str | None = None, reseed: bool = False) -> bool:
        fields: dict = {}
        if title is not None: fields["title"] = title.strip()
        if intent is not None:
            fields["intent"] = intent.strip()
            fields["summary_stale"] = 1     # new goal → re-derive memory + KB connections
            fields["summary_rebuild"] = 1   # a changed goal can recast everything: full rebuild
        if not fields:
            return False
        return self.db.update_thread(thread_id, fields, now_iso())

    # --- the only controls: include/exclude a card, edit/refine the context --

    def set_card_included(self, card_id: str, included: bool) -> bool:
        ep = self.db.get_episode(card_id)
        if not ep:
            return False
        ok = self.db.update_card(card_id, {"included": int(included)})
        if ep.thread_id:
            self._mark_context_stale(ep.thread_id, rebuild=True)  # removing a fact can
        return ok                                                 # invalidate prior memory

    def delete_card(self, card_id: str) -> bool:
        ep = self.db.get_episode(card_id)
        if not ep:
            return False
        self.db.delete_episode(card_id)
        if ep.thread_id:
            self._mark_context_stale(ep.thread_id, rebuild=True)  # full re-derive
        return True

    def set_thread_context(self, thread_id: str, context: str) -> bool:
        """The user hand-edits the context → they own it; stop auto-overwriting."""
        return self.db.update_thread(
            thread_id, {"summary": context, "summary_owned": 1, "summary_stale": 0}, now_iso())

    def refine_context_now(self, thread_id: str) -> dict | None:
        """Hand control back to the AI and re-synthesize NOW from scratch. The manual
        ↻ is a FULL REBUILD (summary_rebuild=1), not an incremental fold — so it
        discards a stale/polluted prior summary and rewrites cleanly from the cards
        in scope, instead of preserving the old text."""
        self.db.update_thread(thread_id, {"summary_owned": 0, "summary_stale": 1,
                                          "summary_rebuild": 1}, now_iso())
        return self.ensure_context(thread_id, force=True)

    def assemble_context(self, thread_id: str, *, query: str | None = None,
                         kb_limit: int = 6, record: bool = True,
                         refine_llm: bool = True) -> dict:
        """Flow B — the state-aware injection. Given where the work stands RIGHT NOW
        (intent + working memory + recent signals + open questions) and, optionally,
        the agent's immediate task (`query`), pull the KB knowledge relevant to this
        moment and assemble a focused, paste-ready package. Unlike the seeded
        background (fixed at thread creation), the KB section is retrieved fresh
        against the current state, so it tracks the work as it moves.
        refine_llm=False (the per-PROMPT hook / MCP pull) uses the stored working
        memory — never blocks the agent's turn on a slow refine."""
        t = self.ensure_context(thread_id, refine_llm=refine_llm)
        if not t:
            return {"brief": "", "intent": "", "working_memory": "",
                    "open_questions": [], "recent": [], "kb": []}
        intent = (t.get("intent") or "").strip()
        memory = (t.get("summary") or "").strip()
        steps = [c for c in self.db.thread_steps(thread_id) if c.included]
        questions = [c.raw_content for c in steps if c.kind == "question"][-6:]
        recent = [self._labelled_item(c) for c in steps
                  if c.kind in ("decision", "requirement", "constraint", "insight")][-5:]

        # Retrieval query = the immediate task (if any) weighted up front, then the
        # current state — so the KB tracks the moment, not the stale initial intent.
        q_parts = []
        if query and query.strip():
            q_parts.append(query.strip())
        q_parts += [p for p in (intent, memory, " ".join(questions)) if p]
        retr_q = "\n".join(q_parts)[:1500]

        results, links, kb_text, kb = [], [], "", []
        if retr_q:
            results, links = self.retrieve(retr_q, limit=kb_limit, scope="main")
            results = [r for r in results if r.relevant]  # gate out off-topic noise
            keep = {r.item.id for r in results}
            links = [(src, it) for src, it in links if src in keep]
            if results:
                from .hooks import _format  # lazy: hooks pulls nothing heavy
                kb_text = _format(results, links)
                kb = [{"id": r.item.id, "title": r.item.title,
                       "summary": r.item.summary, "type": r.item.type,
                       "score": round(r.score, 3)} for r in results]
                if record:
                    ids = [r.item.id for r in results] + [it.id for _, it in links]
                    self.record_usage(ids, retr_q, session=thread_id)

        parts = []
        if query and query.strip():
            parts.append("[CURRENT TASK]\n" + query.strip())
        if intent:
            parts.append("[INTENT — the goal]\n" + intent)
        if memory:
            parts.append("[WORKING MEMORY — decisions, state & learnings]\n" + memory)
        if questions:
            parts.append("[OPEN QUESTIONS]\n" + "\n".join(f"- {q}" for q in questions))
        if kb_text:
            parts.append(kb_text)
        elif t.get("background"):                       # fall back to the seed
            parts.append("[FROM MY KNOWLEDGE BASE]\n" + t["background"].strip())

        bits = []
        if kb: bits.append(f"{len(kb)} fact{'s' if len(kb)!=1 else ''}")
        if memory: bits.append("working memory")
        if questions: bits.append(f"{len(questions)} open q")
        link = self.deep_link(thread_id)
        if parts:
            # clickable breadcrumb at the top — so the agent (and whoever pastes it)
            # can see this came from CRUX and jump to the exact project in one click.
            crumb = "⌁ CRUX · " + (", ".join(bits) or "context")
            if link:
                crumb += f" · {link}"
            parts.insert(0, crumb)
        brief = "\n\n".join(parts)
        if record and brief:
            focus = (query or "").strip()
            self.log_event("pull", thread_id=thread_id, title="Context pulled",
                           detail=(", ".join(bits) or "context") + (f" · {focus[:80]}" if focus else ""),
                           count=len(kb))
        return {"brief": brief, "intent": intent, "link": link,
                "working_memory": memory, "open_questions": questions,
                "recent": recent, "kb": kb}

    def thread_brief(self, thread_id: str, *, query: str | None = None) -> str:
        """The portable, paste-into-any-tool context — the brief slice of the
        state-aware assembly (see assemble_context)."""
        return self.assemble_context(thread_id, query=query)["brief"]

    def finish_thread(self, thread_id: str) -> bool:
        if self.current_thread_id() == thread_id:
            self.db.set_meta("current_thread", None)
        self.close_session_now(thread_id)  # checkpoint the open session as it wraps
        return self.db.update_thread(thread_id, {"status": "done"}, now_iso())

    def delete_thread(self, thread_id: str) -> None:
        if self.current_thread_id() == thread_id:
            self.db.set_meta("current_thread", None)
        self.db.delete_thread(thread_id)

    # A working card's KIND → the durable fact TYPE it becomes when promoted. Cards
    # that aren't durable knowledge (a prompt, a bare note, an open question) map to
    # None and are skipped — only real learnings graduate into the KB.
    _PROMOTE_TYPE = {"decision": "decision", "constraint": "constraint",
                     "requirement": "constraint", "insight": "context",
                     "result": "context", "reference": "reference",
                     "suggestion": "exploration", "architecture": "architecture",
                     "question": None, "prompt": None, "note": None}

    def promote_thread(self, thread_id: str, *, proposed: bool = True) -> list[ContextItem]:
        """Stage a thread's durable learnings for Review — ONE fact per signal card,
        each enriched, placed into the KB tree, and conflict-checked in its node.
        (Was: collapse the whole thread into one blob, which lost every specific.)
        Skips prompts, notes and open questions; the thread itself stays put."""
        cards = [c for c in self.db.thread_steps(thread_id)
                 if c.included and self._PROMOTE_TYPE.get(c.kind, "context")]
        facts: list[ContextItem] = []
        seen: set[str] = set()
        for c in cards[:60]:                       # bound cost on a huge thread
            raw = (c.raw_content or "").strip()
            if not raw:
                continue
            enr = self.processor.enrich(raw)
            enr.type = self._PROMOTE_TYPE.get(c.kind) or enr.type
            f = self._store_fact(raw=raw, enr=enr, episode_id=c.id, locator=None,
                                 source=c.source_ref, scope="individual",
                                 confidence=0.6, proposed=proposed)
            if f.id not in seen:
                seen.add(f.id); facts.append(f)
        return facts

    def current_thread_id(self) -> str | None:
        tid = self.db.get_meta("current_thread")
        if tid and not self.db.get_thread(tid):
            return None
        return tid

    def set_current_thread(self, thread_id: str | None) -> None:
        if thread_id and not self.db.get_thread(thread_id):
            return
        self.db.set_meta("current_thread", thread_id)

    def search(self, query: str, limit: int = 5, include_archived: bool = False,
               scope: str | None = None) -> list[Result]:
        from .embeddings import FakeEmbedding
        qvec = self._embed_query(query)
        return search(self.db, qvec, query, limit=limit,
                      include_archived=include_archived, scope=scope,
                      real_embed=not isinstance(self.embedder, FakeEmbedding))

    def _embed_query(self, text: str) -> list:
        """Embed a query, cached — the agent hook and the 2s poll re-embed the same
        text repeatedly; embeddings are deterministic per model, so cache by text."""
        v = self._qcache.get(text)
        if v is None:
            if len(self._qcache) > 512:    # cheap bound
                self._qcache.clear()
            v = self.embedder.embed(text, input_type="query")
            self._qcache[text] = v
        return v

    def reembed_all(self) -> int:
        """Recompute every fact's embedding with the CURRENT provider — run after
        switching embedders (e.g. fake→NVIDIA) so old and new vectors don't mix
        (cosine of mismatched dimensions is 0, which silently breaks retrieval)."""
        n = 0
        for it in self.db.list(archived=False, limit=100000):
            vec = self.embedder.embed(self._descriptor(
                subject=it.subject, type=it.type, title=it.title, summary=it.summary))
            self.db.set_embedding(it.id, vec, self.embedder.model, now_iso())
            n += 1
        return n

    def retrieve(self, query: str, limit: int = 5, *, expand: bool = True,
                 max_links: int = 4, scope: str | None = None, user: str | None = None):
        """Search + graph expansion — the retrieval payoff. Returns (results, links).

        When `user` is given, retrieval = the shared KB (verified, everyone) PLUS
        that user's OWN private working memory — so an agent gets team truth plus
        the user's session context, but never other people's unverified scratch."""
        if user is not None:
            main_r = self.search(query, limit=limit, scope="main")
            mine_r = [r for r in self.search(query, limit=limit, scope="individual")
                      if r.item.owner == user]
            results = sorted(main_r + mine_r, key=lambda r: r.score, reverse=True)[:limit]
        else:
            results = self.search(query, limit=limit, scope=scope)
        links: list[tuple[str, ContextItem]] = []
        if expand and results:
            seen = {r.item.id for r in results}
            for r in results:
                for edge in self.db.relations_for(r.item.id):
                    if edge["kind"] not in ("extends", "relates"):
                        continue
                    oid = edge["to_id"] if edge["from_id"] == r.item.id else edge["from_id"]
                    if oid in seen:
                        continue
                    o = self.db.get(oid)
                    if not o or o.archived or o.superseded_by:
                        continue
                    seen.add(oid)
                    links.append((r.item.id, o))
                    if len(links) >= max_links:
                        return results, links
        return results, links

    def ask(self, question: str, history: list | None = None, limit: int = 6) -> dict:
        """Chat over the knowledge base: retrieve the top verified facts, synthesize
        a grounded answer that cites them, and return the ranked sources so the user
        can open any one. Multi-turn via the conversation history."""
        question = (question or "").strip()
        if not question:
            return {"answer": "", "sources": []}
        results = self.search(question, limit=limit, scope="main")
        counts = self.db.usage_counts()
        facts, sources = [], []
        for i, r in enumerate(results, 1):
            f = r.item
            facts.append((i, f.title, f.summary, f.raw_content))
            sources.append({"n": i, "id": f.id, "title": f.title, "summary": f.summary,
                            "tier": f.tier, "domain": f.domain, "raw_content": f.raw_content,
                            "source": f.source, "promoted_at": f.promoted_at,
                            "usage_count": counts.get(f.id, 0), "score": round(r.score, 3)})
        answer = self.processor.answer(question, facts, history or [])
        return {"answer": answer, "sources": sources}

    def record_usage(self, item_ids: list[str], query: str, session: str = "",
                     user: str | None = None) -> None:
        """Log that these items were injected into a live agent session — this is
        what powers the dashboard's 'used N times' payoff loop (and, on a shared
        server, who pulled what)."""
        ts = now_iso()
        for iid in item_ids:
            self.db.log_usage(iid, query, session, ts, user=user)

    # --- activity trail: every pull + capture, deep-linked -------------------

    def deep_link(self, thread_id: str | None, card_id: str | None = None) -> str:
        """A clickable URL straight to the exact project (and card) in the
        dashboard — so any activity row, or an agent breadcrumb, is one click from
        the precise page. localhost (not 127.0.0.1) so terminals make it clickable."""
        if not thread_id:
            return ""
        host = "localhost" if self.cfg.host in ("127.0.0.1", "0.0.0.0", "") else self.cfg.host
        base = f"http://{host}:{self.cfg.port}/#/project/{thread_id}"
        return f"{base}/card/{card_id}" if card_id else base

    def log_event(self, kind: str, *, thread_id: str | None = None,
                  session_id: str | None = None, title: str, detail: str = "",
                  count: int = 0) -> None:
        """Record one activity event (pull|capture). Best-effort: never let a
        logging failure break capture/injection."""
        try:
            self.db.log_event(kind, thread_id, session_id, title, detail, count, now_iso())
        except Exception:
            pass

    def activity(self, limit: int = 50, thread_id: str | None = None) -> list[dict]:
        """The activity timeline with a deep link attached to each row."""
        out = []
        for e in self.db.recent_events(limit, thread_id=thread_id):
            out.append({**e, "link": self.deep_link(e.get("thread_id"))})
        return out

    # --- contradiction-aware writes (the "neighborhood update") --------------

    NEIGH_THRESHOLD = 0.80   # global scan: only check genuinely close neighbors
    HEURISTIC_FLAG = 0.90    # global, offline (no LLM judge): flag above this similarity
    # Within a fact's own KB node the candidates are already about the same thing,
    # so a contradiction is meaningful at lower similarity (differently-worded
    # opposites) — scan wider and flag (offline) at a lower bar than the global scan.
    NODE_NEIGH_THRESHOLD = 0.55
    NODE_HEURISTIC_FLAG = 0.72
    MAX_CHECK = 3            # cap judgments per new fact (bounds cost on bulk ingest)

    def _detect_conflicts(self, item: ContextItem) -> None:
        """When a fact lands, scan its neighborhood and flag likely
        contradictions — LLM-judged with a real key, similarity-based offline.
        Scoped to the fact's KB-tree subtree when it has one (apples-to-apples;
        fewer false positives, catches differently-worded opposites), else a global
        neighborhood scan. Never auto-resolves; records candidates for Review."""
        from .embeddings import cosine
        vec = self.db.embedding_of(item.id)
        if not vec:
            return
        if item.subject_path:
            ids = self.db.subtree_item_ids(item.subject_path)
            candidates = ((oid, self.db.embedding_of(oid)) for oid in ids)
            neigh, flag = self.NODE_NEIGH_THRESHOLD, self.NODE_HEURISTIC_FLAG
        else:
            candidates = self.db.all_embeddings()  # non-archived only
            neigh, flag = self.NEIGH_THRESHOLD, self.HEURISTIC_FLAG
        sims = []
        for oid, ovec in candidates:
            if oid == item.id or not ovec:
                continue
            s = cosine(vec, ovec)
            if s >= neigh:
                sims.append((s, oid))
        sims.sort(reverse=True)
        for s, oid in sims[: self.MAX_CHECK]:
            other = self.db.get(oid)
            if not other or other.superseded_by:
                continue
            if other.source_episode_id and other.source_episode_id == item.source_episode_id:
                continue  # facts from the same document aren't contradictions
            verdict, reason = self.processor.judge_contradiction(item.summary, other.summary)
            flagged = (s >= flag) if verdict is None else verdict
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

        # Only NOMINATED items reach Review — private working memory stays out of it.
        working = [i for i in self.db.list(scope="individual", archived=False, limit=500)
                   if not i.superseded_by and i.proposed]
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

            reason = ""
            if it.id in conflict_of:
                cf = conflict_of[it.id]
                status, relation = "conflict", "update"   # a contradiction = an update
                related = dict(cf["other"], similarity=cf["similarity"])
                reason = cf.get("reason") or ""
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
                    # Similarity heuristic only — NO model call here. /review is a hot
                    # read path the dashboard polls; a synchronous LLM call would block
                    # it (and hang for a minute if a provider is set but unreachable).
                    relation = "duplicate" if s >= self.DUP_SIM else "extend"
                    reason = f"{int(s * 100)}% similar to an existing fact"
                    status = "attention"

            d["status"] = status
            d["fit"] = {"relation": relation, "related": related, "reason": reason}
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
        if relation == "update":
            return f"Updates '{short(related and related.get('title'))}' — promoting should supersede the old version."
        if relation == "duplicate":
            return f"Near-duplicate of '{short(related['title'])}' ({pct}). You may already have this."
        if relation == "extend":
            return f"Extends '{short(related['title'])}' — adds detail; both kept and linked."
        return f"New {it.type or 'note'}. Nothing similar yet."

    # --- promotion: the refinement gate (individual -> main) -----------------

    AUTO_LINK_SIM = 0.82   # auto-connect genuinely-related verified facts
    AUTO_LINK_MAX = 3      # cap edges per promotion so the graph stays a graph, not a hairball

    def promote(self, item_id: str, *, title: str | None = None,
                summary: str | None = None, type: str | None = None,
                tier: str | None = None, domain: str | None = None,
                subject: str | None = None, confidence: float = 0.95) -> bool:
        """Move a working item into the verified `main` graph, optionally refining
        its fields. Then auto-link it to genuinely-related verified facts so the
        graph organizes itself (no manual filing)."""
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
        if tier is not None:
            fields["tier"] = tier
        if domain is not None:
            fields["domain"] = domain
        if subject is not None:
            fields["subject"] = subject.strip().lower()
        ok = self.db.update(full, fields, now_iso())
        if ok:
            if any(x is not None for x in (title, summary, type, subject)):  # descriptor changed → re-embed
                it = self.db.get(full)
                vec = self.embedder.embed(self._descriptor(
                    subject=it.subject, type=it.type, title=it.title, summary=it.summary))
                self.db.set_embedding(full, vec, self.embedder.model, now_iso())
            self._detect_conflicts(self.db.get(full))  # now verified — recheck vs truth
            self._auto_link(full)                       # connect to related verified facts
        return ok

    def _auto_link(self, item_id: str) -> None:
        """Create 'relates' edges from this verified fact to its nearest verified
        neighbors (the graph self-organising across domains/topics)."""
        from .embeddings import cosine
        vec = self.db.embedding_of(item_id)
        if not vec:
            return
        existing = {(r["to_id"] if r["from_id"] == item_id else r["from_id"])
                    for r in self.db.relations_for(item_id)}
        scored = []
        for oid, ovec in self.db.all_embeddings():
            if not ovec or oid == item_id or oid in existing:
                continue
            o = self.db.get(oid)
            if not o or o.archived or o.superseded_by or o.scope != "main":
                continue
            s = cosine(vec, ovec)
            if s >= self.AUTO_LINK_SIM:
                scored.append((s, oid))
        scored.sort(reverse=True)
        for s, oid in scored[: self.AUTO_LINK_MAX]:
            self.db.add_relation(item_id, oid, "relates", f"{int(s * 100)}% related", now_iso())

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
            self.db.delete_relations_for(full)
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

    # --- graph: extend edges -------------------------------------------------

    def extend(self, new_id: str, target_id: str, *, reason: str = "",
               promote: bool = True) -> bool:
        """Record that `new_id` EXTENDS `target_id` (both kept, linked). The edge
        is created here, on approval — never automatically. Optionally promote the
        new fact into the main graph in the same step."""
        new_full = self.db.resolve_id(new_id)
        target_full = self.db.resolve_id(target_id)
        if not new_full or not target_full or new_full == target_full:
            return False
        self.db.add_relation(new_full, target_full, "extends", reason, now_iso())
        if promote:
            self.promote(new_full)
        return True

    def relations_of(self, item_id: str) -> dict:
        """Edges for an item, split into what it extends vs what extends it —
        each enriched with the other item's title/scope for display."""
        full = self.db.resolve_id(item_id) or item_id
        extends, extended_by, related = [], [], []
        for r in self.db.relations_for(full):
            other_id = r["to_id"] if r["from_id"] == full else r["from_id"]
            other = self.db.get(other_id)
            if not other or other.archived:
                continue
            entry = {"id": other.id, "title": other.title, "scope": other.scope,
                     "tier": other.tier, "domain": other.domain, "reason": r["reason"]}
            if r["kind"] == "relates":
                related.append(entry)
            elif r["kind"] == "extends":
                (extends if r["from_id"] == full else extended_by).append(entry)
        return {"extends": extends, "extended_by": extended_by, "related": related}

    # --- lenses (emergent topics: named views over the graph) ----------------

    def create_lens(self, name: str, intent: str = "") -> int:
        return self.db.add_lens(name.strip(), (intent or "").strip(), now_iso())

    def list_lenses(self) -> list[dict]:
        return self.db.list_lenses()

    def delete_lens(self, lens_id: int) -> None:
        self.db.delete_lens(int(lens_id))

    def lens_item_ids(self, lens_id: int, limit: int = 60) -> list[str]:
        """Gather the verified facts relevant to a lens (by its name+intent) plus
        their graph neighbors — a fact surfaces in every lens it's relevant to."""
        lens = self.db.get_lens(int(lens_id))
        if not lens:
            return []
        q = f"{lens['name']} {lens.get('intent') or ''}".strip()
        results = self.search(q, limit=limit, scope="main")
        ids = {r.item.id for r in results}
        for r in list(results):                      # one hop along the graph
            for edge in self.db.relations_for(r.item.id):
                if edge["kind"] not in ("extends", "relates"):
                    continue
                oid = edge["to_id"] if edge["from_id"] == r.item.id else edge["from_id"]
                if oid in ids:
                    continue
                o = self.db.get(oid)
                if o and not o.archived and not o.superseded_by and o.scope == "main":
                    ids.add(oid)
        return list(ids)

    def related(self, item_id: str, limit: int = 6) -> list[dict]:
        """Semantic neighbors in the verified graph — facts related by meaning
        even without an explicit edge ('See also'). Excludes self + hard-linked."""
        from .embeddings import cosine
        full = self.db.resolve_id(item_id)
        if not full:
            return []
        vec = self.db.embedding_of(full)
        if not vec:
            return []
        linked = {(r["to_id"] if r["from_id"] == full else r["from_id"])
                  for r in self.db.relations_for(full)}
        linked.add(full)
        scored = []
        for oid, ovec in self.db.all_embeddings():
            if not ovec or oid in linked:
                continue
            o = self.db.get(oid)
            if not o or o.archived or o.superseded_by or o.scope != "main":
                continue
            scored.append((cosine(vec, ovec), o))
        scored.sort(key=lambda x: -x[0])
        return [{"id": o.id, "title": o.title, "tier": o.tier, "scope": o.scope,
                 "score": round(s, 3)} for s, o in scored[:limit] if s >= 0.4]


def _extract_url(text: str) -> str | None:
    m = re.search(r"https?://\S+", text)
    return m.group(0) if m else None
