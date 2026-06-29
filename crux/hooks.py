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
        # If a project is active, inject the STATE-AWARE package (Flow B): the
        # current working memory + open questions + KB knowledge relevant to this
        # prompt, not just a bare KB lookup. Otherwise fall back to plain retrieval.
        tid = store.current_thread_id()
        if tid:
            # per-prompt → fast path: never block the turn on a slow refine
            ctx = store.assemble_context(tid, query=prompt, kb_limit=MAX_ITEMS,
                                         refine_llm=False)["brief"]
            store.close()
            print(json.dumps({"additionalContext": ctx}) if ctx else "{}")
            return 0
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


def _emit_start(ctx: str) -> None:
    if ctx:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": ctx}}))
    else:
        print("{}")


def hook_session_start() -> int:
    """SessionStart hook. Injects the active project's resume brief — intent,
    working memory, open questions, and the KB knowledge relevant to where things
    stand — so the agent picks up exactly where you left off, with zero input.
    Crash-safe: any error prints {} and exits 0 so it can never break startup."""
    try:
        cfg = Config.load()
        if cfg.server:
            # team mode: per-prompt injection still runs; skip the start brief here
            print("{}")
            return 0
        store = Store(cfg)
        tid = store.current_thread_id()
        ctx = store.assemble_context(tid)["brief"] if tid else ""
        store.close()
        _emit_start(ctx)
        return 0
    except Exception:
        print("{}")
        return 0


def _last_assistant_text(path: str) -> tuple[str, str]:
    """The text of the agent's most recent response in a Claude Code transcript
    (JSONL), plus its uuid for dedupe. Tool-use blocks are ignored — we want the
    prose where the agent states what it decided/did."""
    last_text, last_uuid = "", ""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get("type") != "assistant":
                    continue
                content = (ev.get("message") or {}).get("content")
                parts = []
                if isinstance(content, list):
                    parts = [b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text"]
                elif isinstance(content, str):
                    parts = [content]
                txt = " ".join(p for p in parts if p).strip()
                if txt:
                    last_text = txt
                    last_uuid = ev.get("uuid") or ev.get("requestId") or ""
    except OSError:
        return "", ""
    return last_text, last_uuid


def hook_capture() -> int:
    """Stop hook — the automatic capture path. Reads the turn's transcript, pulls
    the decisions/requirements/results/open-questions out of the agent's last
    response, and files them into the ACTIVE project's working memory, tagged
    'via claude-code'. No input required.

    Safe by contract: runs only when a project is active (so it can't pollute a
    stray store), dedupes per session so the same turn isn't captured twice, and on
    ANY error prints {} and exits 0 so it can never break a turn.
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tpath = payload.get("transcript_path")
        session = payload.get("session_id", "")
        if not tpath:
            print("{}")
            return 0
        cfg = Config.load()
        if cfg.server:
            print("{}")           # team-mode capture flows through /hook elsewhere
            return 0
        store = Store(cfg)
        tid = store.current_thread_id()
        if not tid:
            store.close()
            print("{}")
            return 0
        text, uuid = _last_assistant_text(tpath)
        seen_key = f"capture_seen:{session}"
        if not text or store.db.get_meta(seen_key) == (uuid or text[:60]):
            store.close()
            print("{}")
            return 0
        res = store.ingest_working(text, thread_id=tid, source="agent",
                                   source_ref="claude-code", split=True,
                                   signals_only=True)  # keep decisions/results, not chatter
        store.db.set_meta(seen_key, uuid or text[:60])
        n = res.get("count", 0)
        link = store.deep_link(tid)
        store.close()
        if n:
            crumb = f"⌁ CRUX · captured {n} signal{'s' if n != 1 else ''} → working memory"
            if link:
                crumb += f" · {link}"
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "Stop", "additionalContext": crumb}}))
        else:
            print("{}")
        return 0
    except Exception:
        # Never break the user's turn because of CRUX.
        print("{}")
        return 0
