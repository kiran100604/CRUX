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


def _line(i, *, indent="• ", note="") -> str:
    tier = "VERIFIED" if i.scope == "main" else "working"
    cite = i.source or i.id[:8]
    return f"{indent}[{tier}/{i.type}] {i.title} — {i.summary}{note} [src: {cite}]"


def _format(results, links=None) -> str:
    """Render retrieved facts, with their connected (extends) facts nested under
    them so the agent sees the enriched picture, not isolated fragments."""
    by_parent: dict = {}
    for pid, it in (links or []):
        by_parent.setdefault(pid, []).append(it)
    lines = ["[CRUX CONTEXT] (retrieved automatically — verify before trusting)"]
    for r in results:
        lines.append(_line(r.item))
        for li in by_parent.get(r.item.id, []):
            lines.append(_line(li, indent="   ↳ connected: ", note=""))
    lines.append("[END CRUX CONTEXT]")
    return "\n".join(lines)


def hook_inject() -> int:
    """UserPromptSubmit hook. stdin: Claude Code JSON. stdout: {additionalContext}."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        prompt = payload.get("prompt") or payload.get("user_prompt") or ""
        if not prompt.strip():
            print("{}")
            return 0
        store = Store(Config.load())
        results, links = store.retrieve(prompt, limit=MAX_ITEMS)
        if results:
            # record the payoff: these items (and their connected facts) just helped
            ids = [r.item.id for r in results] + [it.id for _, it in links]
            store.record_usage(ids, prompt, session=payload.get("session_id", ""))
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
