"""MCP server — the fallback injection path for agents without lifecycle hooks,
and for explicit on-demand queries. Talks straight to the DB via Store; it does
not depend on the FastAPI server running.

Requires the optional `mcp` extra:  pip install "crux[mcp]"
"""

from __future__ import annotations

from .config import Config
from .store import Store


def run() -> None:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:  # pragma: no cover
        raise SystemExit("MCP SDK not installed. Run: pip install 'crux[mcp]'") from e

    store = Store(Config.load())
    mcp = FastMCP("crux")

    @mcp.tool()
    def get_context(query: str, limit: int = 5, include_archived: bool = False) -> dict:
        """Fetch relevant project context from CRUX for the current task."""
        results = store.search(query, limit=limit, include_archived=include_archived)
        return {
            "items": [
                {"id": r.item.id, "title": r.item.title, "summary": r.item.summary,
                 "type": r.item.type, "source": r.item.source, "score": round(r.score, 4)}
                for r in results
            ],
            "returned": len(results),
        }

    @mcp.tool()
    def add_context(content: str, type: str | None = None, pin: bool = False) -> dict:
        """Add a piece of context to CRUX from inside an agent session.

        Agent-written items default to LOW confidence (staged for human review in
        the dashboard) unless pinned, so an over-eager agent can't pollute the store.
        """
        item = store.capture(content, type_hint=type, pinned=pin,
                             confidence=0.9 if pin else 0.4)
        return {"id": item.id, "title": item.title, "summary": item.summary}

    mcp.run()
