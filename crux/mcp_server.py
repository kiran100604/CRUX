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
    def get_context(query: str, limit: int = 5, scope: str = "all",
                    include_archived: bool = False) -> dict:
        """Fetch relevant project context from CRUX for the current task.

        scope: "all" (default — verified main graph prioritized, recent working
        notes included), "main" (verified truth only), or "individual" (working
        layer only).
        """
        s = None if scope == "all" else scope
        # include_archived isn't graph-expanded; otherwise use the payoff retrieval
        if include_archived:
            results = store.search(query, limit=limit, scope=s, include_archived=True)
            links = []
        else:
            results, links = store.retrieve(query, limit=limit, scope=s)
        items = [
            {"id": r.item.id, "title": r.item.title, "summary": r.item.summary,
             "type": r.item.type, "tier": r.item.tier, "scope": r.item.scope,
             "source": r.item.source, "score": round(r.score, 4)}
            for r in results
        ]
        connected = [
            {"id": it.id, "title": it.title, "summary": it.summary, "type": it.type,
             "tier": it.tier, "scope": it.scope, "connected_to": pid[:8], "via": "extends"}
            for pid, it in links
        ]
        return {"items": items, "connected": connected, "returned": len(results)}

    @mcp.tool()
    def add_context(content: str, type: str | None = None) -> dict:
        """Log a piece of context from inside an agent session.

        Agent-written items always land in the WORKING (individual) layer at low
        confidence — they are "what the agent is doing", not verified truth. A human
        refines and promotes them to the main graph later, so an over-eager agent
        can never pollute the trusted layer.
        """
        item = store.capture(content, type_hint=type, scope="individual",
                             confidence=0.4, source_type="agent")
        return {"id": item.id, "title": item.title, "summary": item.summary, "scope": item.scope}

    mcp.run()
