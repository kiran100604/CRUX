"""Claude Code hook entrypoints — the primary, reliable injection path.

`hook_inject` runs on every `UserPromptSubmit`: it retrieves relevant context and
returns it via `additionalContext`, so the model never has to decide to call a
tool. It is crash-safe by contract: on ANY error it prints `{}` and exits 0 so it
can never break a turn.
"""

from __future__ import annotations

import json
import sys

from .config import Config
from .store import Store

MAX_ITEMS = 5

# Group retrieved facts into a directive brief so the agent treats them as
# guardrails for the task — not a passive pile of references.
_BUCKET = {
    "constraint": "CONSTRAINTS TO HONOR (hard rules)",
    "decision": "DECISIONS ALREADY MADE",
    "architecture": "ARCHITECTURE & DESIGN",
    "design": "ARCHITECTURE & DESIGN",
}
_DEFAULT_BUCKET = "PRODUCT & CONTEXT"
_ORDER = ["CONSTRAINTS TO HONOR (hard rules)", "DECISIONS ALREADY MADE",
          "ARCHITECTURE & DESIGN", "PRODUCT & CONTEXT"]
_HEADER = ("[CRUX TEAM CONTEXT — apply this to the task. Treat constraints as hard "
           "rules, honor decisions already made, follow the documented architecture, "
           "and call out anything in the request that conflicts with the below.]")


def _line(i) -> str:
    tier = "verified" if i.scope == "main" else "working"
    cite = i.source or i.id[:8]
    return f"• {i.title} — {i.summary} [{tier}; src: {cite}]"


def _format(results, links=None) -> str:
    """Directive brief: facts grouped into constraints / decisions / architecture /
    product-context, so the agent applies them rather than just reading them.
    Connected (extends) facts are folded into the same buckets."""
    buckets: dict = {}
    items = [r.item for r in results] + [it for _, it in (links or [])]
    seen = set()
    for it in items:
        if it.id in seen:
            continue
        seen.add(it.id)
        buckets.setdefault(_BUCKET.get(it.type, _DEFAULT_BUCKET), []).append(it)
    lines = [_HEADER]
    for b in _ORDER:
        if buckets.get(b):
            lines.append(f"\n{b}:")
            lines.extend(_line(it) for it in buckets[b])
    lines.append("\n[END CRUX TEAM CONTEXT]")
    return "\n".join(lines)


def hook_inject() -> int:
    """UserPromptSubmit hook. stdin: Claude Code JSON. stdout: {additionalContext}."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        prompt = payload.get("prompt") or payload.get("user_prompt") or ""
        if not prompt.strip():
            print("{}")
            return 0
        cfg = Config.load()
        session = payload.get("session_id", "")
        if cfg.server:
            # team mode: pull from the shared intent graph over HTTP
            from . import client
            try:
                resp = client.retrieve(cfg.server, prompt, session=session,
                                       user=cfg.user, limit=MAX_ITEMS)
            except Exception:
                print("{}")  # server unreachable → never break the turn
                return 0
            ctx = resp.get("context") or ""
            print(json.dumps({"additionalContext": ctx}) if ctx else "{}")
            return 0
        store = Store(cfg)
        results, links = store.retrieve(prompt, limit=MAX_ITEMS, user=cfg.user)
        if results:
            # record the payoff: these items (and their connected facts) just helped
            ids = [r.item.id for r in results] + [it.id for _, it in links]
            store.record_usage(ids, prompt, session=session, user=cfg.user)
        store.close()
        if not results:
            print("{}")
            return 0
        print(json.dumps({"additionalContext": _format(results, links)}))
        return 0
    except Exception:
        # Never break the user's turn because of CRUX.
        print("{}")
        return 0


def hook_capture() -> int:
    """Stop hook stub — reads transcript_path and (later) stages candidate facts.

    Ships as a no-op in v1: we earn trust on inject first, then turn on capture so
    an over-eager extractor can't pollute the store before retrieval is trusted.
    """
    try:
        json.loads(sys.stdin.read() or "{}")
    except Exception:
        pass
    print("{}")
    return 0
