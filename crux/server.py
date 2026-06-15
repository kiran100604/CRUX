"""FastAPI backend for the dashboard. ONE port; the SPA is served from here too.
Exists only for the human UI — the inject (hook) and MCP paths never need it.

Requires the optional `server` extra:  pip install "crux[server]"
"""

# NOTE: no `from __future__ import annotations` here — FastAPI must see the real
# Pydantic model objects as body annotations, not stringized forward refs.

from pathlib import Path

from .config import Config
from .store import Store

STATIC_DIR = Path(__file__).parent / "static"


def create_app(cfg: Config):
    try:
        from fastapi import FastAPI
        from fastapi.responses import FileResponse
        from pydantic import BaseModel
    except ImportError as e:  # pragma: no cover
        raise SystemExit("FastAPI not installed. Run: pip install 'crux[server]'") from e

    store = Store(cfg)
    app = FastAPI(title="CRUX")

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
        return FileResponse(STATIC_DIR / "index.html")

    class IngestIn(BaseModel):
        content: str
        source_ref: str | None = None
        source_type: str = "paste"

    @app.post("/capture")
    def capture(body: CaptureIn):
        item = store.capture(body.content, source=body.source,
                             type_hint=body.type, scope=body.scope)
        return _enrich([item])[0]

    @app.post("/ingest")
    def ingest(body: IngestIn):
        res = store.ingest(body.content, source_type=body.source_type,
                           source_ref=body.source_ref)
        return {"episode_id": res["episode"].id,
                "facts": _enrich(res["facts"])}

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
