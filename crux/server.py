"""FastAPI backend for the dashboard. ONE port; the SPA is served from here too.
Exists only for the human UI — the inject (hook) and MCP paths never need it.

Requires the optional `server` extra:  pip install "crux[server]"
"""

# NOTE: no `from __future__ import annotations` here — FastAPI must see the real
# Pydantic model objects as body annotations, not stringized forward refs.

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import Config
from .store import Store

STATIC_DIR = Path(__file__).parent / "static"


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


def create_app(cfg: Config):
    try:
        from fastapi import FastAPI
        from fastapi.responses import FileResponse, RedirectResponse
        from pydantic import BaseModel
    except ImportError as e:  # pragma: no cover
        raise SystemExit("FastAPI not installed. Run: pip install 'crux[server]'") from e

    store = Store(cfg)
    app = FastAPI(title="CRUX")
    app.state.executor = ThreadPoolExecutor(max_workers=1)  # serial background ingest

    class CaptureIn(BaseModel):
        content: str
        type: str | None = None
        source: str | None = None
        scope: str = "individual"

    class PromoteIn(BaseModel):
        title: str | None = None
        summary: str | None = None
        type: str | None = None

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

    class SetupIn(BaseModel):
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
            "hook_installed": hook,
            "default_mods": ["cmd" if plat == "darwin" else "ctrl", "shift"],
            "default_key": "space",
        }

    @app.post("/api/setup")
    def setup_apply(body: SetupIn):
        from .config import save_env_file
        from .hotkey import chord_label, write_snippets
        from .install import claude_settings_path, install_claude_hook

        vals: dict[str, str] = {}
        if body.anthropic_key:
            vals["ANTHROPIC_API_KEY"] = body.anthropic_key.strip()
            vals["CRUX_PROCESSING_PROVIDER"] = "anthropic"
        if body.openai_key:
            vals["OPENAI_API_KEY"] = body.openai_key.strip()
            vals["CRUX_EMBEDDING_PROVIDER"] = "openai"
        mods = body.chord_mods or list(cfg.hotkey_mods)
        key = body.chord_key or cfg.hotkey_key
        vals["CRUX_HOTKEY_MODS"] = ",".join(mods)
        vals["CRUX_HOTKEY_KEY"] = key
        save_env_file(cfg.home, vals)

        hook_status = "skipped"
        if body.install_hook:
            hook_status = install_claude_hook(claude_settings_path(globally=True))

        write_snippets(cfg.home / "hotkey", mods, key)
        cfg.mark_configured()
        return {
            "ok": True,
            "hook": hook_status,
            "chord": chord_label(mods, key),
            "keys_saved": [k for k in vals if k.endswith("_API_KEY")],
        }

    class IngestIn(BaseModel):
        content: str
        source_ref: str | None = None
        source_type: str = "paste"

    class EditIn(BaseModel):
        title: str | None = None
        summary: str | None = None
        type: str | None = None

    @app.post("/capture")
    def capture(body: CaptureIn):
        item = store.capture(body.content, source=body.source,
                             type_hint=body.type, scope=body.scope)
        return _enrich([item])[0]

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
    def edit(item_id: str, body: EditIn):
        return {"ok": store.edit(item_id, title=body.title,
                                 summary=body.summary, type=body.type)}

    @app.get("/search")
    def search(q: str, limit: int = 5, scope: str | None = None):
        results = store.search(q, limit=limit, scope=_scope(scope))
        enriched = {d["id"]: d for d in _enrich([r.item for r in results])}
        return {"items": [{"score": r.score, **enriched[r.item.id]} for r in results]}

    @app.get("/items")
    def items(type: str | None = None, scope: str | None = None, archived: bool = False):
        return {"items": _enrich(store.db.list(type=type, scope=_scope(scope),
                                               archived=archived, limit=1000))}

    @app.get("/stats")
    def stats():
        main = len(store.db.list(scope="main", limit=100000))
        working = len(store.db.list(scope="individual", limit=100000))
        total_uses = sum(store.db.usage_counts().values())
        return {"crystals": main, "fluid": working, "injections": total_uses}

    @app.get("/feed")
    def feed(limit: int = 20):
        return {"usages": store.db.recent_usages(limit)}

    @app.get("/review")
    def review():
        r = store.review()
        return {"working": _enrich(r["working"]), "conflicts": r["conflicts"]}

    @app.post("/items/{item_id}/promote")
    def promote(item_id: str, body: PromoteIn):
        return {"ok": store.promote(item_id, title=body.title,
                                    summary=body.summary, type=body.type)}

    @app.post("/items/{item_id}/demote")
    def demote(item_id: str):
        return {"ok": store.demote(item_id)}

    @app.post("/items/{item_id}/archive")
    def archive(item_id: str, restore: bool = False):
        return {"ok": store.archive(item_id, value=not restore)}

    @app.post("/items/{item_id}/supersede")
    def supersede(item_id: str, new_id: str):
        return {"ok": store.supersede(item_id, new_id)}

    @app.post("/conflicts/{conflict_id}/dismiss")
    def dismiss(conflict_id: int):
        return {"ok": store.dismiss_conflict(conflict_id)}

    return app


def run(cfg: Config) -> None:
    import uvicorn

    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port)
