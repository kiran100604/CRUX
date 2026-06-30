"""FastAPI backend for the dashboard. ONE port; the SPA is served from here too.
Exists only for the human UI — the inject (hook) and MCP paths never need it.

Requires the optional `server` extra:  pip install "crux[server]"
"""

# NOTE: no `from __future__ import annotations` here — FastAPI must see the real
# Pydantic model objects as body annotations, not stringized forward refs.

import os
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import Config
from .models import now_iso
from .store import Store

STATIC_DIR = Path(__file__).parent / "static"


def _admin_token(cfg: Config) -> str:
    """The leader's token. Env override, else a generated one stored in CRUX home.
    Whoever can read this file (the person running the server) is the leader."""
    env = os.environ.get("CRUX_ADMIN_TOKEN")
    if env:
        return env
    p = cfg.home / "admin_token"
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    cfg.ensure_home()
    tok = secrets.token_urlsafe(24)
    p.write_text(tok, encoding="utf-8")
    return tok


def _bg_process(cfg: Config, episode_id: str, content: str,
                source_type: str, source_ref: str | None) -> None:
    """Run heavy document extraction off the request thread, on its own DB
    connection (WAL makes concurrent access with request threads safe)."""
    worker = Store(cfg)
    try:
        worker.process_episode(episode_id, content, source_type=source_type,
                               source_ref=source_ref)
    finally:
        worker.close()


def _promote_thread(cfg: Config, thread_id: str) -> None:
    """Distill a thread's learnings into Review off the request path — the
    extraction runs an LLM per card, so it must never block the button (a slow or
    unreachable model used to hang it for up to a minute = 'nothing happened')."""
    worker = Store(cfg)
    try:
        worker.promote_thread(thread_id)
    finally:
        worker.close()


def _route_pending(cfg: Config, thread_id: str) -> None:
    """Classify ALL unrouted cards in the thread in one call, then refresh context —
    off the request path (own DB connection), so capture returns instantly and a
    burst of dumps costs a single routing call."""
    if not thread_id:
        return
    worker = Store(cfg)
    try:
        worker.route_pending(thread_id)
        worker.ensure_context(thread_id)
    finally:
        worker.close()


def create_app(cfg: Config):
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import FileResponse, RedirectResponse
        from pydantic import BaseModel
    except ImportError as e:  # pragma: no cover
        raise SystemExit("FastAPI not installed. Run: pip install 'crux[server]'") from e

    token = _admin_token(cfg)

    def _is_leader(request: "Request") -> bool:
        # the person running the server (localhost) is the leader; remote callers
        # must present the admin token. (Assumes a direct connection, no proxy.)
        host = request.client.host if request.client else ""
        if host in ("127.0.0.1", "::1", "localhost"):
            return True
        return secrets.compare_digest(request.headers.get("x-crux-token", ""), token)

    def _leader(request: "Request") -> None:
        if not _is_leader(request):
            raise HTTPException(status_code=403,
                                detail="leader only — connect with the admin token to validate the KB")

    _t0 = time.time()
    store = Store(cfg)
    print(f"[crux] Store init (db + embedder + processor): {time.time()-_t0:.2f}s "
          f"| processing={cfg.processing_provider} embedding={cfg.embedding_provider}",
          file=sys.stderr, flush=True)
    app = FastAPI(title="CRUX")
    app.state.executor = ThreadPoolExecutor(max_workers=4)  # background ingest/route/refine
    app.state.routing = set()    # thread ids with an in-flight auto-route (dedupe polls)
    app.state.refreshing = set() # thread ids with an in-flight summary refresh
    app.state.promoting = set()  # thread ids with an in-flight learnings promotion
    app.state.bg_lock = __import__("threading").Lock()  # guards the dedupe sets

    @app.middleware("http")
    async def _timing(request, call_next):
        # Diagnostic: log every request's wall time so a slow page load points at
        # the exact endpoint. Local-first app → logs go to the `crux start` terminal.
        t = time.time()
        resp = await call_next(request)
        dt = (time.time() - t) * 1000
        mark = " <<< SLOW" if dt > 500 else ""
        print(f"[crux] {request.method} {request.url.path} {dt:.0f}ms{mark}",
              file=sys.stderr, flush=True)
        return resp

    class CaptureIn(BaseModel):
        content: str
        type: str | None = None
        source: str | None = None
        source_ref: str | None = None  # provenance label (which tool/agent it came from)
        scope: str = "individual"
        user: str | None = None
        proposed: bool = True   # True → leader's Review; False → private working memory
        as_step: bool = False   # True → land in working memory as a thread step (raw)
        thread_id: str | None = None
        kind: str | None = None  # user tag (decision/reference/…) → born classified, treated by role
        role: str | None = None  # the user-facing role word (Prompt/Info/Idea/…) shown on the card

    class PromoteIn(BaseModel):
        title: str | None = None
        summary: str | None = None
        type: str | None = None
        tier: str | None = None
        domain: str | None = None
        subject: str | None = None

    def _scope(s: str | None):
        return None if s in (None, "all") else s

    def _enrich(items):
        """Attach usage stats so the UI can glow crystals by real impact."""
        counts = store.db.usage_counts()
        last = store.db.usage_last()
        out = []
        for i in items:
            d = i.to_public_dict()
            d["usage_count"] = counts.get(i.id, 0)
            d["last_used"] = last.get(i.id)
            out.append(d)
        return out

    @app.get("/")
    def index():
        # First run → send people through the in-browser setup wizard.
        if not cfg.is_configured():
            return RedirectResponse("/setup")
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/setup")
    def setup_page():
        return FileResponse(STATIC_DIR / "setup.html")

    @app.get("/static/{name}")
    def static_file(name: str):
        # serve design assets (tokens.css, …) from the static dir; no path traversal
        from fastapi.responses import FileResponse as _FR
        p = (STATIC_DIR / name).resolve()
        if p.parent != STATIC_DIR.resolve() or not p.exists() or not p.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return _FR(p)

    class SetupIn(BaseModel):
        nvidia_key: str | None = None      # one key → LLM + embeddings (NVIDIA NIM)
        anthropic_key: str | None = None
        openai_key: str | None = None
        install_hook: bool = True
        chord_mods: list[str] | None = None
        chord_key: str | None = None

    @app.get("/api/setup")
    def setup_state():
        import sys as _sys
        from .install import claude_settings_path, hook_present
        plat = "linux" if _sys.platform.startswith("linux") else _sys.platform
        try:
            import json as _json
            sp = claude_settings_path(globally=True)
            hook = hook_present(_json.loads(sp.read_text(encoding="utf-8"))) if sp.exists() else False
        except Exception:
            hook = False
        return {
            "platform": plat,
            "configured": cfg.is_configured(),
            "has_anthropic": bool(cfg.anthropic_api_key),
            "has_openai": bool(cfg.openai_api_key),
            "has_nvidia": bool(cfg.openai_api_key) and "nvidia" in (cfg.api_base or ""),
            "hook_installed": hook,
            "default_mods": ["cmd" if plat == "darwin" else "ctrl", "shift"],
            "default_key": "space",
        }

    @app.post("/api/setup")
    def setup_apply(body: SetupIn):
        from .config import save_env_file
        from .hotkey import chord_label, valid_chord, write_snippets
        from .install import claude_settings_path, install_claude_hook

        vals: dict[str, str] = {}
        # NVIDIA NIM: one key powers BOTH the LLM and embeddings (OpenAI-compatible)
        if body.nvidia_key:
            k = body.nvidia_key.strip()
            vals.update({
                "CRUX_PROCESSING_PROVIDER": "openai", "OPENAI_API_KEY": k,
                "CRUX_OPENAI_BASE_URL": "https://integrate.api.nvidia.com/v1",
                "CRUX_PROCESSING_MODEL": "meta/llama-3.3-70b-instruct",
                "CRUX_EMBEDDING_PROVIDER": "openai",
                "CRUX_EMBEDDING_MODEL": "nvidia/nv-embedqa-e5-v5",
            })
        # other providers (override per-channel if given alongside)
        if body.anthropic_key:
            vals["ANTHROPIC_API_KEY"] = body.anthropic_key.strip()
            vals["CRUX_PROCESSING_PROVIDER"] = "anthropic"
        if body.openai_key:
            vals["OPENAI_API_KEY"] = body.openai_key.strip()
            vals["CRUX_EMBEDDING_PROVIDER"] = "openai"
        # validate the chord; on an invalid custom chord, keep the current one
        mods = body.chord_mods or list(cfg.hotkey_mods)
        key = body.chord_key or cfg.hotkey_key
        ok, reason = valid_chord(mods, key)
        if not ok:
            mods, key = list(cfg.hotkey_mods), cfg.hotkey_key  # fall back, don't persist a bad chord
        vals["CRUX_HOTKEY_MODS"] = ",".join(mods)
        vals["CRUX_HOTKEY_KEY"] = key
        save_env_file(cfg.home, vals)

        hook_status = "skipped"
        if body.install_hook:
            hook_status = install_claude_hook(claude_settings_path(globally=True))

        write_snippets(cfg.home / "hotkey", mods, key)
        cfg.mark_configured()

        # Go live immediately — rebuild the running store's providers from the new
        # config and re-embed existing facts so old (offline) and new vectors don't
        # mix. Best-effort: a bad key / network issue must not break setup.
        provider_status = "offline"
        if body.nvidia_key or body.anthropic_key or body.openai_key:
            try:
                store.reload_providers()
                provider_status = f"{store.cfg.processing_provider}/{store.embedder.model}"
                if store.cfg.embedding_provider != "fake":
                    store.reembed_all()
            except Exception as e:
                provider_status = f"saved, applies on restart ({str(e)[:60]})"

        # Linux: a changed chord must be re-bound at the compositor level or the
        # old shortcut stays active. Auto-rebind so the new chord works immediately.
        rebound = None
        import sys as _sys
        if _sys.platform == "linux":
            from .hotkey import bind_gnome
            rebound, _ = bind_gnome(mods, key, f"{_sys.executable} -m crux.cli capture")

        # Drop the taskbar capture icon as part of first-run setup, so it's ready
        # without a separate step (best-effort — never block setup if it fails).
        launcher = None
        try:
            from .launcher import install_launcher
            launcher = install_launcher(cfg.home)
        except Exception:
            launcher = None
        return {
            "ok": True,
            "hook": hook_status,
            "chord": chord_label(mods, key),
            "chord_rejected": (None if ok else reason),
            "rebound": rebound,
            "providers": provider_status,
            "keys_saved": [k for k in vals if k.endswith("_API_KEY")],
            "launcher": launcher,
        }

    class IngestIn(BaseModel):
        content: str
        source_ref: str | None = None
        source_type: str = "paste"

    class EditIn(BaseModel):
        title: str | None = None
        summary: str | None = None
        type: str | None = None
        tier: str | None = None
        domain: str | None = None
        subject: str | None = None
        subject_path: str | None = None

    @app.post("/capture")
    def capture(body: CaptureIn):
        # Human captures (hotkey/popup/dashboard) land in working memory as a raw
        # step on the current thread — kept as narrative, not atomized into facts.
        if body.as_step or body.thread_id:
            res = store.add_step(body.content, source=body.source or "note",
                                 source_ref=body.source_ref,
                                 thread_id=body.thread_id, kind=body.kind,
                                 role=body.role)
            # route (classify + file) off the request path so capture stays instant;
            # batches ALL unrouted cards on the thread → one call for a burst of dumps.
            # A user-tagged dump is born classified, so there's nothing to route.
            if res.get("thread_id") and not res.get("tagged"):
                tid = res["thread_id"]
                _schedule_bg(tid, app.state.routing, lambda: _route_pending(cfg, tid))
            return {"step": True, **res}
        item = store.capture(body.content, source=body.source,
                             type_hint=body.type, scope=body.scope,
                             owner=body.user or cfg.user, proposed=body.proposed)
        return _enrich([item])[0]

    @app.post("/launcher/install")
    def launcher_install():
        # "Add to taskbar": drop an OS launcher icon that opens the tag-capture
        # popup. Click it (after copying text) → pick a tag. Nothing to set up.
        from .launcher import install_launcher
        return install_launcher(cfg.home)

    class HookIn(BaseModel):
        content: str
        project: str | None = None   # thread id or exact active title; else current
        source: str = "agent"        # the channel (agent/hotkey/note/…)
        source_ref: str | None = None  # provenance label (which tool/agent)
        type: str | None = None      # optional kind hint (unused when splitting)
        split: bool = True           # break a transcript into discrete typed entries
        create: bool = False         # start the project if the name is unknown

    @app.post("/hook")
    def hook(body: HookIn):
        # The drop-in for live agent hooks and transcript pastes: a chunk of work
        # → one or more typed, source-tagged entries in a project's working memory.
        # Splitting/classification run inline (off the hotkey path) so a hook fires
        # once and gets clean signals back.
        tid = store.resolve_thread(body.project, create=body.create)
        res = store.ingest_working(body.content, thread_id=tid,
                                   source=body.source, source_ref=body.source_ref,
                                   split=body.split)
        return {"ok": True, **res}

    @app.post("/ingest")
    def ingest(body: IngestIn):
        # Episode saved synchronously (so we can return its id); facts extracted
        # in the background so a big document never blocks the request.
        ep = store.create_episode(body.content, source_type=body.source_type,
                                  source_ref=body.source_ref)
        app.state.executor.submit(_bg_process, cfg, ep.id, body.content,
                                  body.source_type, body.source_ref)
        return {"episode_id": ep.id, "status": "processing"}

    @app.post("/items/{item_id}/edit")
    def edit(item_id: str, body: EditIn, request: Request):
        _leader(request)
        return {"ok": store.edit(item_id, title=body.title,
                                 summary=body.summary, type=body.type,
                                 tier=body.tier, domain=body.domain, subject=body.subject,
                                 subject_path=body.subject_path)}

    @app.get("/search")
    def search(q: str, limit: int = 5, scope: str | None = None):
        results = store.search(q, limit=limit, scope=_scope(scope))
        enriched = {d["id"]: d for d in _enrich([r.item for r in results])}
        return {"items": [{"score": r.score, **enriched[r.item.id]} for r in results]}

    @app.get("/items")
    def items(type: str | None = None, scope: str | None = None, archived: bool = False):
        out = _enrich(store.db.list(type=type, scope=_scope(scope),
                                    archived=archived, limit=1000))
        for d in out:                       # attach graph edges for display
            d["edges"] = store.relations_of(d["id"])
        return {"items": out}

    @app.get("/related")
    def related(id: str, limit: int = 6):
        return {"items": store.related(id, limit)}

    @app.get("/tree")
    def tree():
        # the KB taxonomy: every node with its direct + recursive fact counts, so
        # the dashboard can render the knowledge base as a browsable doc tree
        return {"nodes": store.tree()}

    @app.get("/tree/facts")
    def tree_facts(path: str):
        # the facts living at a node or anywhere beneath it (subtree view)
        return {"path": path, "items": _enrich_paths(store.node_facts(path))}

    def _enrich_paths(items: list[dict]) -> list[dict]:
        counts = store.db.usage_counts()
        last = store.db.usage_last()
        for d in items:
            d["usage_count"] = counts.get(d["id"], 0)
            d["last_used"] = last.get(d["id"])
        return items

    class AskIn(BaseModel):
        question: str
        history: list[dict] = []

    @app.post("/ask")
    def ask(body: AskIn):
        # chat over the knowledge base: grounded answer + ranked cited sources
        return store.ask(body.question, history=body.history)

    @app.get("/fact/{fid}")
    def fact(fid: str):
        # full detail for a surfaced source: raw text, filing, provenance, links
        it = store.db.get(store.db.resolve_id(fid) or fid)
        if not it:
            raise HTTPException(status_code=404, detail="no such fact")
        d = _enrich([it])[0]
        d["edges"] = store.relations_of(it.id)
        d["related"] = store.related(it.id, 6)
        return d

    @app.get("/working")
    def working():
        # this machine's private working memory (the person running the dashboard)
        return {"items": store.working_memory(owner=cfg.user), "user": cfg.user}

    @app.post("/items/{item_id}/nominate")
    def nominate(item_id: str):
        # owner pushing their own scratch to Review — promotion is still leader-gated
        return {"ok": store.nominate(item_id)}

    class LensIn(BaseModel):
        name: str
        intent: str | None = None

    @app.get("/lenses")
    def lenses():
        return {"lenses": store.list_lenses()}

    @app.post("/lenses")
    def create_lens(body: LensIn, request: Request):
        _leader(request)
        return {"id": store.create_lens(body.name, body.intent or "")}

    @app.delete("/lenses/{lens_id}")
    def delete_lens(lens_id: int, request: Request):
        _leader(request)
        store.delete_lens(lens_id)
        return {"ok": True}

    @app.get("/lenses/{lens_id}/items")
    def lens_items(lens_id: int):
        return {"ids": store.lens_item_ids(lens_id)}

    # --- threads: the working layer ("what I'm doing now") -------------------
    class ThreadIn(BaseModel):
        title: str | None = None
        intent: str | None = None

    class ContextIn(BaseModel):
        context: str

    class IncludeIn(BaseModel):
        included: bool

    @app.get("/threads")
    def threads():
        return {"threads": store.list_threads(), "current": store.current_thread_id()}

    @app.post("/threads")
    def create_thread(body: ThreadIn):
        t = store.create_thread(body.title or "", body.intent or "")
        return store.thread_view(t["id"])

    @app.get("/threads/{thread_id}")
    def get_thread(thread_id: str):
        t = store.thread_view(thread_id, refine_llm=False)   # fast: no blocking LLM
        if not t:
            raise HTTPException(status_code=404, detail="no such thread")
        # Auto-sort any unrouted cards in the background — captures from the popup
        # (and any other direct path) bypass /capture's router, so without this they
        # would stay "sorting…" forever.
        if any(not c["routed"] for c in t.get("cards", [])):
            _schedule_bg(thread_id, app.state.routing,
                         lambda: _route_pending(cfg, thread_id))
        # Refresh the working-memory summary in the BACKGROUND when it's stale, so a
        # slow LLM never blocks this page load. The fast path returned the stored
        # summary; the next poll will show the refreshed one.
        if t.get("summary_stale"):
            _schedule_refresh(thread_id)
        return t

    def _schedule_bg(key: str, inflight: set, fn) -> None:
        """Run fn in the background, but at most ONE per key at a time — the
        check-and-claim is locked so two near-simultaneous polls can't both fire it
        (the source of duplicate LLM calls)."""
        with app.state.bg_lock:
            if key in inflight:
                return
            inflight.add(key)

        def _job():
            try:
                fn()
            except Exception as e:
                print(f"[crux] background task failed ({key[:8]}): {e}", file=sys.stderr)
            finally:
                with app.state.bg_lock:
                    inflight.discard(key)
        app.state.executor.submit(_job)

    def _schedule_refresh(thread_id: str) -> None:
        _schedule_bg(thread_id, app.state.refreshing,
                     lambda: store.ensure_context(thread_id, force=True))

    @app.post("/threads/{thread_id}/edit")
    def edit_thread(thread_id: str, body: ThreadIn):
        store.edit_thread(thread_id, title=body.title, intent=body.intent,
                          reseed=body.intent is not None)
        return store.thread_view(thread_id)

    @app.post("/threads/{thread_id}/context")
    def set_context(thread_id: str, body: ContextIn):
        # the user hand-edits the living context → they own it from now on
        return {"ok": store.set_thread_context(thread_id, body.context)}

    @app.post("/threads/{thread_id}/refine")
    def refine_context(thread_id: str):
        # Hand control back to the AI and re-synthesize from scratch — in the
        # BACKGROUND so the ↻ button returns instantly. Flag a full rebuild now;
        # the worker does the LLM call; the poll shows the result.
        store.db.update_thread(thread_id, {"summary_owned": 0, "summary_stale": 1,
                                           "summary_rebuild": 1}, now_iso())
        _schedule_refresh(thread_id)          # deduped: one in-flight refine per thread
        # refine_llm=False → return instantly; the scheduled worker is the ONLY LLM
        # call. (Without this the default thread_view would refine synchronously too,
        # blocking the request AND duplicating the call.)
        return store.thread_view(thread_id, refine_llm=False) or {}

    @app.post("/cards/{card_id}/include")
    def card_include(card_id: str, body: IncludeIn):
        return {"ok": store.set_card_included(card_id, body.included)}

    @app.delete("/cards/{card_id}")
    def card_delete(card_id: str):
        return {"ok": store.delete_card(card_id)}

    @app.get("/threads/{thread_id}/brief")
    def thread_brief(thread_id: str, q: str | None = None):
        return {"brief": store.thread_brief(thread_id, query=q)}

    class AssembleIn(BaseModel):
        query: str | None = None   # the agent's immediate task, to focus retrieval
        kb_limit: int = 6

    @app.post("/threads/{thread_id}/assemble")
    def assemble_context(thread_id: str, body: AssembleIn):
        # Flow B: state-aware injection — what does the agent need RIGHT NOW?
        pkg = store.assemble_context(thread_id, query=body.query, kb_limit=body.kb_limit)
        if not pkg["brief"] and not store.db.get_thread(thread_id):
            raise HTTPException(status_code=404, detail="no such thread")
        return pkg

    @app.post("/threads/{thread_id}/current")
    def make_current(thread_id: str):
        store.set_current_thread(thread_id)
        return {"ok": True, "current": store.current_thread_id()}

    @app.post("/threads/{thread_id}/finish")
    def finish_thread(thread_id: str):
        return {"ok": store.finish_thread(thread_id)}

    @app.post("/threads/{thread_id}/promote")
    def promote_thread(thread_id: str, request: Request):
        # Run the (LLM-heavy, per-card) distillation in the BACKGROUND so the button
        # returns instantly and can't hang. The staged facts land in Review; the
        # dashboard's poll surfaces them. Deduped so a double-click fires once.
        if not store.db.get_thread(thread_id):
            raise HTTPException(status_code=404, detail="no such thread")
        promotable = sum(
            1 for c in store.db.thread_steps(thread_id)
            if c.included and store._PROMOTE_TYPE.get(c.kind, "context"))
        _schedule_bg(thread_id, app.state.promoting,
                     lambda: _promote_thread(cfg, thread_id))
        return {"status": "distilling", "promotable": promotable}

    @app.delete("/threads/{thread_id}")
    def delete_thread(thread_id: str):
        store.delete_thread(thread_id)
        return {"ok": True}

    @app.get("/unsorted")
    def unsorted():
        return {"steps": [e.to_public_dict() for e in store.db.unsorted_steps()]}

    class RetrieveIn(BaseModel):
        prompt: str
        session: str | None = None
        user: str | None = None
        limit: int = 5

    @app.post("/retrieve")
    def retrieve(body: RetrieveIn):
        """Team-mode entrypoint: a client's hook/CLI sends a prompt; we retrieve,
        build the directive brief, record usage (tagged by user), return the brief."""
        from .hooks import _format
        results, links = store.retrieve(body.prompt, limit=body.limit, user=body.user or None)
        if not results:
            return {"context": "", "count": 0}
        ids = [r.item.id for r in results] + [it.id for _, it in links]
        store.record_usage(ids, body.prompt, session=body.session or "",
                           user=body.user or None)
        return {"context": _format(results, links), "count": len(results)}

    @app.get("/stats")
    def stats():
        main = len(store.db.list(scope="main", limit=100000))
        working = len(store.db.list(scope="individual", limit=100000))
        total_uses = sum(store.db.usage_counts().values())
        return {"crystals": main, "fluid": working, "injections": total_uses}

    @app.get("/feed")
    def feed(limit: int = 20):
        return {"usages": store.db.recent_usages(limit)}

    @app.get("/activity")
    def activity(limit: int = 60, thread: str | None = None):
        # the always-on, deep-linked trail of every pull + capture
        return {"events": store.activity(limit, thread_id=thread)}

    @app.get("/review")
    def review():
        items = store.triage()
        counts = {"clean": sum(1 for i in items if i["status"] == "clean"),
                  "attention": sum(1 for i in items if i["status"] == "attention"),
                  "conflict": sum(1 for i in items if i["status"] == "conflict")}
        return {"inbox": items, "conflicts": store.open_conflicts(), "counts": counts}

    @app.get("/api/whoami")
    def whoami(request: Request):
        from .hotkey import chord_label
        try:
            chord = chord_label(cfg.hotkey_mods, cfg.hotkey_key)
        except Exception:
            chord = ""
        return {"leader": _is_leader(request), "user": cfg.user, "hotkey": chord}

    @app.post("/promote-clean")
    def promote_clean(request: Request):
        _leader(request)
        ids = [i["id"] for i in store.triage() if i["status"] == "clean"]
        for i in ids:
            store.promote(i)
        return {"promoted": len(ids)}

    @app.post("/items/{item_id}/promote")
    def promote(item_id: str, body: PromoteIn, request: Request):
        _leader(request)
        return {"ok": store.promote(item_id, title=body.title, summary=body.summary,
                                    type=body.type, tier=body.tier, domain=body.domain,
                                    subject=body.subject)}

    class ExtendIn(BaseModel):
        target_id: str
        reason: str | None = None
        promote: bool = True

    @app.post("/items/{item_id}/extend")
    def extend(item_id: str, body: ExtendIn, request: Request):
        _leader(request)
        return {"ok": store.extend(item_id, body.target_id,
                                   reason=body.reason or "", promote=body.promote)}

    @app.post("/items/{item_id}/demote")
    def demote(item_id: str, request: Request):
        _leader(request)
        return {"ok": store.demote(item_id)}

    @app.post("/items/{item_id}/archive")
    def archive(item_id: str, request: Request, restore: bool = False):
        _leader(request)
        return {"ok": store.archive(item_id, value=not restore)}

    @app.post("/items/{item_id}/supersede")
    def supersede(item_id: str, new_id: str, request: Request):
        _leader(request)
        return {"ok": store.supersede(item_id, new_id)}

    @app.post("/conflicts/{conflict_id}/dismiss")
    def dismiss(conflict_id: int, request: Request):
        _leader(request)
        return {"ok": store.dismiss_conflict(conflict_id)}

    return app


def run(cfg: Config) -> None:
    import time as _t
    import uvicorn

    t0 = _t.time()
    print("[crux] importing web stack…", file=sys.stderr, flush=True)
    app = create_app(cfg)
    print(f"[crux] app ready in {_t.time()-t0:.2f}s — starting server on "
          f"http://{cfg.host}:{cfg.port}", file=sys.stderr, flush=True)
    uvicorn.run(app, host=cfg.host, port=cfg.port)
