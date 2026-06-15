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


def _format(results) -> str:
    lines = ["[CRUX CONTEXT] (retrieved automatically — verify before trusting)"]
    for r in results:
        i = r.item
        tag = i.type.upper()
        cite = i.source or i.id[:8]
        lines.append(f"• ({tag}) {i.title} — {i.summary} [src: {cite}]")
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
        results = store.search(prompt, limit=MAX_ITEMS)
        store.close()
        if not results:
            print("{}")
            return 0
        print(json.dumps({"additionalContext": _format(results)}))
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
