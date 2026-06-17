"""MCP server — lets any agent both PULL the team's context and PUSH back what it
decided, so capture stops depending on a human remembering to do it.

Two tools:
  • get_context  — pull the directive brief for the current task.
  • log_work     — at the end of a task, record the decisions/knowledge made so
                   they enter Review (a human validates before they're trusted).

Team-aware: when CRUX_SERVER is set, reads/writes go to the shared graph; else
local. Requires the optional `mcp` extra:  pip install "crux[mcp]"
"""

from __future__ import annotations

from .config import Config
from .store import Store


def run() -> None:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:  # pragma: no cover
        raise SystemExit("MCP SDK not installed. Run: pip install 'crux[mcp]'") from e

    cfg = Config.load()
    store = None if cfg.server else Store(cfg)
    mcp = FastMCP("crux")

    def _stage(content: str, type_hint: str | None, session: str) -> dict:
        """Stage one proposal into the working layer (never the trusted KB)."""
        if cfg.server:
            from . import client
            r = client.capture(cfg.server, content, type=type_hint,
                               source="agent", user=cfg.user)
            return {"title": r.get("title", content[:48])}
        item = store.capture(content, type_hint=type_hint, scope="individual",
                             confidence=0.4, source="agent", source_type="agent")
        return {"title": item.title}

    @mcp.tool()
    def get_context(task: str, limit: int = 6) -> dict:
        """Pull the team's relevant knowledge for the task you're about to do.

        Returns a directive brief — constraints to honor, decisions already made,
        architecture, and product context. Apply it: treat constraints as hard
        rules, follow decisions, and flag anything in the request that conflicts.
        Call this BEFORE starting work.
        """
        if cfg.server:
            from . import client
            r = client.retrieve(cfg.server, task, user=cfg.user, limit=limit)
            return {"context": r.get("context", ""), "count": r.get("count", 0)}
        from .hooks import _format
        results, links = store.retrieve(task, limit=limit)
        if not results:
            return {"context": "", "count": 0}
        ids = [r.item.id for r in results] + [it.id for _, it in links]
        store.record_usage(ids, task, user=cfg.user)
        return {"context": _format(results, links), "count": len(results)}

    @mcp.tool()
    def log_work(decisions: list[str] = [], constraints: list[str] = [],
                 knowledge: list[str] = [], session: str = "") -> dict:
        """Record what this session produced, so it enters the team's knowledge.

        Call this when you FINISH a meaningful unit of work (or whenever you make a
        notable choice). Capture only durable, reusable things — not step-by-step
        chatter:
          • decisions  — choices made ("chose Stripe over Razorpay for fees")
          • constraints — rules to honor going forward ("never call billing sync directly")
          • knowledge  — facts worth remembering ("the auth token expires in 15m")
        Everything you log is a PROPOSAL: it lands in Review for a human to validate
        before it becomes trusted knowledge — so log generously, it can't pollute
        the KB.
        """
        staged, titles = 0, []
        for c in decisions:
            if c and c.strip():
                titles.append(_stage(c, "decision", session)["title"]); staged += 1
        for c in constraints:
            if c and c.strip():
                titles.append(_stage(c, "constraint", session)["title"]); staged += 1
        for c in knowledge:
            if c and c.strip():
                titles.append(_stage(c, "context", session)["title"]); staged += 1
        return {"staged": staged, "titles": titles,
                "note": "Proposed to Review — a human will validate before it's trusted."}

    mcp.run()
