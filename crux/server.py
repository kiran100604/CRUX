"""FastAPI backend for the dashboard. ONE port; the SPA is served from here too.
Exists only for the human UI — the inject (hook) and MCP paths never need it.

Requires the optional `server` extra:  pip install "crux[server]"
"""

from __future__ import annotations

from .config import Config
from .store import Store


def create_app(cfg: Config):
    try:
        from fastapi import FastAPI
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

    @app.post("/capture")
    def capture(body: CaptureIn):
        item = store.capture(body.content, source=body.source,
                             type_hint=body.type, scope=body.scope)
        return item.to_public_dict()

    @app.get("/search")
    def search(q: str, limit: int = 5, scope: str | None = None):
        return {"items": [{"score": r.score, **r.item.to_public_dict()}
                          for r in store.search(q, limit=limit, scope=_scope(scope))]}

    @app.get("/items")
    def items(type: str | None = None, scope: str | None = None, archived: bool = False):
        return {"items": [i.to_public_dict()
                          for i in store.db.list(type=type, scope=_scope(scope), archived=archived)]}

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

    return app


def run(cfg: Config) -> None:
    import uvicorn

    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port)
