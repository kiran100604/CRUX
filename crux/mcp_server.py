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

    def _stage(content: str, type_hint: str | None, proposed: bool) -> dict:
        """Stage one item. proposed=True → the leader's Review; proposed=False →
        the user's PRIVATE working memory (never reviewed, expires)."""
        if cfg.server:
            from . import client
            r = client.capture(cfg.server, content, type=type_hint,
                               source="agent", user=cfg.user, proposed=proposed)
            return {"title": r.get("title", content[:48])}
        item = store.capture(content, type_hint=type_hint, scope="individual",
                             confidence=0.4, source="agent", source_type="agent",
                             owner=cfg.user, proposed=proposed)
        return {"title": item.title}

    # Type hints from log_work map onto the card-kind taxonomy so an MCP capture is
    # treated by role exactly like a hook/popup one (decision = ours/authoritative,
    # reference/knowledge = context to be aware of, never our decision).
    _MCP_KIND = {"decision": "decision", "constraint": "constraint",
                 "requirement": "requirement", "context": "insight",
                 "reference": "reference", None: None}

    def _to_working(content: str, type_hint: str | None) -> None:
        """Also fold an agent capture into the ACTIVE project's LIVE working memory
        (local mode) — same path as the Stop hook: born classified by its kind, so
        the living context updates incrementally and role-aware. No-op in team mode
        or with no active project."""
        if cfg.server or not store:
            return
        tid = store.current_thread_id()
        if not tid:
            return
        try:
            store.add_step(content, source="agent", source_ref="mcp",
                           thread_id=tid, kind=_MCP_KIND.get(type_hint))
        except Exception:
            pass  # working-memory update is best-effort; never fail the agent's call

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
        # If a project is active, return the STATE-AWARE package (working memory +
        # open questions + KB relevant to the task); else a bare KB lookup.
        tid = store.current_thread_id()
        if tid:
            pkg = store.assemble_context(tid, query=task, kb_limit=limit)
            return {"context": pkg["brief"], "count": len(pkg["kb"]),
                    "open_in_crux": pkg.get("link", "")}
        from .hooks import _format
        results, links = store.retrieve(task, limit=limit, user=cfg.user)
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
                titles.append(_stage(c, "decision", True)["title"])
                _to_working(c, "decision"); staged += 1
        for c in constraints:
            if c and c.strip():
                titles.append(_stage(c, "constraint", True)["title"])
                _to_working(c, "constraint"); staged += 1
        for c in knowledge:
            if c and c.strip():
                titles.append(_stage(c, "context", True)["title"])
                _to_working(c, "context"); staged += 1
        return {"staged": staged, "titles": titles,
                "note": "Proposed to Review (durable knowledge) and folded into the "
                        "active project's working memory, classified by role."}

    @mcp.tool()
    def remember(notes: list[str] = []) -> dict:
        """Save private working memory for THIS user — your session scratch so you
        don't have to re-explain context next time (what you explored, errors hit,
        current state, half-formed ideas). This is NOT reviewed and NOT shared; it's
        private, and it expires. Use log_work (not this) for durable team knowledge.
        """
        n = 0
        for c in notes:
            if c and c.strip():
                _stage(c, None, False)
                _to_working(c, None); n += 1   # also fold into the live working context (auto-classified)
        return {"remembered": n, "note": "Private working memory (not reviewed, expires)."}

    mcp.run()
