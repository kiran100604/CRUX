"""CLI — `cortex <command>`. Thin shell over Store; no logic of its own."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config
from .store import Store


def _store() -> Store:
    return Store(Config.load())


def _print_item(item, score=None):
    score_s = f"  ({score:.3f})" if score is not None else ""
    pin = " 📌" if item.pinned else ""
    print(f"[{item.type}] {item.title}{pin}{score_s}")
    print(f"    {item.summary}")
    print(f"    id={item.id[:8]}  tags={','.join(item.tags)}  {item.captured_at[:10]}")
    if item.source:
        print(f"    src: {item.source}")


def cmd_add(args):
    store = _store()
    content = args.text
    if args.file:
        content = Path(args.file).read_text()
    if not content:
        content = sys.stdin.read()
    item = store.capture(content, source=args.source, type_hint=args.type, pinned=args.pin)
    print(f"✓ captured: {item.title}  (id={item.id[:8]}, type={item.type})")
    store.close()


def cmd_query(args):
    store = _store()
    results = store.search(args.query, limit=args.limit)
    if not results:
        print("no results")
    for r in results:
        _print_item(r.item, r.score)
        print()
    store.close()


def cmd_list(args):
    store = _store()
    pinned = True if args.pinned else None
    for item in store.db.list(type=args.type, pinned=pinned, archived=args.archived):
        _print_item(item)
        print()
    store.close()


def cmd_pin(args):
    store = _store()
    ok = store.pin(args.id, value=not args.off)
    print("✓ updated" if ok else "not found")
    store.close()


def cmd_archive(args):
    store = _store()
    ok = store.archive(args.id, value=not args.restore)
    print("✓ updated" if ok else "not found")
    store.close()


def cmd_supersede(args):
    store = _store()
    try:
        ok = store.supersede(args.old, args.new)
        print("✓ superseded" if ok else "old item not found")
    except ValueError as e:
        print(f"error: {e}")
    store.close()


def cmd_status(args):
    cfg = Config.load()
    store = _store()
    total = len(store.db.list(limit=100000))
    print(f"db:         {cfg.db_path}")
    print(f"items:      {total}")
    print(f"embeddings: {cfg.embedding_provider} ({store.embedder.model})")
    print(f"processing: {cfg.processing_provider} ({cfg.processing_model})")
    print(f"hook:       {'installed' if _hook_installed() else 'not installed'} "
          f"(run `cortex install-hook`)")
    store.close()


def cmd_hook_inject(args):
    from .hooks import hook_inject
    raise SystemExit(hook_inject())


def cmd_hook_capture(args):
    from .hooks import hook_capture
    raise SystemExit(hook_capture())


def cmd_install_hook(args):
    """Explicit, transparent hook install — prints exactly what it writes."""
    settings = Path(args.settings).expanduser()
    block = {
        "matcher": "",
        "statusMessage": "Loading CORTEX context...",
        "hooks": [{"type": "command", "command": "cortex hook-inject"}],
    }
    data = json.loads(settings.read_text()) if settings.exists() else {}
    hooks = data.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    if any("cortex hook-inject" in h.get("command", "")
           for entry in hooks for h in entry.get("hooks", [])):
        print("hook already installed in", settings)
        return
    hooks.append(block)
    print(f"Will write the following UserPromptSubmit hook to {settings}:\n")
    print(json.dumps(block, indent=2))
    if not args.yes and input("\nProceed? [y/N] ").strip().lower() != "y":
        print("aborted")
        return
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2))
    print("✓ installed. CORTEX now injects context on every prompt in this project.")


def cmd_serve(args):
    from .server import run
    run(Config.load())


def cmd_mcp(args):
    from .mcp_server import run
    run()


def _hook_installed(settings: str = ".claude/settings.json") -> bool:
    p = Path(settings)
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text())
        return "cortex hook-inject" in json.dumps(data)
    except Exception:
        return False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cortex", description="Local context layer for AI coding agents.")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="capture text into context")
    a.add_argument("text", nargs="?", default="")
    a.add_argument("--file"); a.add_argument("--source"); a.add_argument("--type")
    a.add_argument("--pin", action="store_true")
    a.set_defaults(func=cmd_add)

    q = sub.add_parser("query", help="hybrid search")
    q.add_argument("query"); q.add_argument("--limit", type=int, default=5)
    q.set_defaults(func=cmd_query)

    l = sub.add_parser("list", help="list items")
    l.add_argument("--type"); l.add_argument("--pinned", action="store_true")
    l.add_argument("--archived", action="store_true")
    l.set_defaults(func=cmd_list)

    pin = sub.add_parser("pin"); pin.add_argument("id"); pin.add_argument("--off", action="store_true")
    pin.set_defaults(func=cmd_pin)

    ar = sub.add_parser("archive"); ar.add_argument("id"); ar.add_argument("--restore", action="store_true")
    ar.set_defaults(func=cmd_archive)

    su = sub.add_parser("supersede"); su.add_argument("old"); su.add_argument("new")
    su.set_defaults(func=cmd_supersede)

    sub.add_parser("status").set_defaults(func=cmd_status)

    ih = sub.add_parser("install-hook", help="install the UserPromptSubmit hook (explicit)")
    ih.add_argument("--settings", default=".claude/settings.json")
    ih.add_argument("--yes", action="store_true")
    ih.set_defaults(func=cmd_install_hook)

    sub.add_parser("hook-inject").set_defaults(func=cmd_hook_inject)
    sub.add_parser("hook-capture").set_defaults(func=cmd_hook_capture)
    sub.add_parser("serve").set_defaults(func=cmd_serve)
    sub.add_parser("mcp").set_defaults(func=cmd_mcp)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
