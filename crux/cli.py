"""CLI — `crux <command>`. Thin shell over Store; no logic of its own."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import Config
from .store import Store


def _store() -> Store:
    return Store(Config.load())


def _scope_arg(value: str | None) -> str | None:
    """'all' (or unset) -> None (both tiers); otherwise the chosen tier."""
    return None if value in (None, "all") else value


def _print_item(item, score=None):
    score_s = f"  ({score:.3f})" if score is not None else ""
    badge = " ★main" if item.scope == "main" else " ·working"
    print(f"[{item.type}]{badge} {item.title}{score_s}")
    print(f"    {item.summary}")
    print(f"    id={item.id[:8]}  tags={','.join(item.tags)}  {item.captured_at[:10]}")
    if item.source:
        print(f"    src: {item.source}")


def cmd_add(args):
    store = _store()
    scope = "main" if args.main else "individual"
    if args.file:
        res = store.ingest_file(args.file, scope=scope)
        facts = res["facts"]
        print(f"✓ ingested {res['episode'].source_ref} → {len(facts)} fact(s) staged for review")
        for f in facts[:12]:
            loc = f" · {f.locator}" if f.locator else ""
            print(f"   • [{f.type}] {f.title}{loc}")
        if len(facts) > 12:
            print(f"   … and {len(facts)-12} more")
    else:
        content = args.text or sys.stdin.read()
        item = store.capture(content, source=args.source, type_hint=args.type, scope=scope)
        print(f"✓ captured: {item.title}  (id={item.id[:8]}, type={item.type}, scope={item.scope})")
    store.close()


def cmd_capture(args):
    """Hotkey entrypoint: grab the clipboard and capture it. Bind a global
    shortcut to `crux capture` (see README for Raycast/Hammerspoon snippets)."""
    store = _store()
    text = read_clipboard()
    if not text or not text.strip():
        print("clipboard is empty — nothing to capture")
        store.close(); return
    if len(text) > 400 or any(ln.lstrip().startswith("#") for ln in text.splitlines()):
        res = store.ingest(text, source_type="paste", source_ref="clipboard")
        print(f"✓ captured clipboard → {len(res['facts'])} fact(s) staged for review")
    else:
        item = store.capture(text, source_type="hotkey")
        print(f"✓ captured: {item.title}  (id={item.id[:8]})")
    store.close()


def read_clipboard() -> str:
    import shutil, subprocess
    for cmd in (["pbpaste"], ["wl-paste", "-n"],
                ["xclip", "-selection", "clipboard", "-o"], ["xsel", "-b"]):
        if shutil.which(cmd[0]):
            try:
                return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
            except Exception:
                pass
    if shutil.which("powershell"):
        try:
            return subprocess.run(["powershell", "-command", "Get-Clipboard"],
                                  capture_output=True, text=True, timeout=5).stdout
        except Exception:
            pass
    return ""


def cmd_query(args):
    store = _store()
    results = store.search(args.query, limit=args.limit, scope=_scope_arg(args.scope))
    if not results:
        print("no results")
    for r in results:
        _print_item(r.item, r.score)
        print()
    store.close()


def cmd_list(args):
    store = _store()
    for item in store.db.list(type=args.type, scope=_scope_arg(args.scope), archived=args.archived):
        _print_item(item)
        print()
    store.close()


def cmd_promote(args):
    store = _store()
    ok = store.promote(args.id, title=args.title, summary=args.summary, type=args.type)
    print("✓ promoted to main graph" if ok else "not found")
    store.close()


def cmd_edit(args):
    store = _store()
    ok = store.edit(args.id, title=args.title, summary=args.summary, type=args.type)
    print("✓ edited (re-embedded, conflicts rechecked)" if ok else "not found")
    store.close()


def cmd_demote(args):
    store = _store()
    ok = store.demote(args.id)
    print("✓ moved back to working layer" if ok else "not found")
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


def cmd_conflicts(args):
    store = _store()
    conflicts = store.open_conflicts()
    if not conflicts:
        print("no open conflicts — your verified knowledge is consistent ✓")
        store.close(); return
    print(f"{len(conflicts)} possible contradiction(s):\n")
    for c in conflicts:
        a, b = c["a"], c["b"]
        reason = c.get("reason") or ""
        extra = f" · {reason}" if reason and "similar" not in reason else ""
        print(f"  #{c['id']}  ({int(c['similarity']*100)}% similar{extra})")
        print(f"    A [{a['id'][:8]}] {a['title']}")
        print(f"    B [{b['id'][:8]}] {b['title']}")
        print(f"    resolve:  crux supersede {b['id'][:8]} {a['id'][:8]}   (keep A)")
        print(f"              crux supersede {a['id'][:8]} {b['id'][:8]}   (keep B)")
        print(f"              crux dismiss {c['id']}                      (not a conflict)\n")
    store.close()


def cmd_dismiss(args):
    store = _store()
    store.dismiss_conflict(args.id)
    print("✓ dismissed — won't resurface")
    store.close()


def cmd_status(args):
    cfg = Config.load()
    store = _store()
    main = len(store.db.list(scope="main", limit=100000))
    working = len(store.db.list(scope="individual", limit=100000))
    print(f"db:         {cfg.db_path}")
    print(f"items:      {main + working}  (main: {main}, working: {working})")
    print(f"embeddings: {cfg.embedding_provider} ({store.embedder.model})")
    print(f"processing: {cfg.processing_provider} ({cfg.processing_model})")
    print(f"hook:       {'installed' if _hook_installed() else 'not installed'} "
          f"(run `crux install-hook`)")
    store.close()


def cmd_hook_inject(args):
    from .hooks import hook_inject
    raise SystemExit(hook_inject())


def cmd_hook_capture(args):
    from .hooks import hook_capture
    raise SystemExit(hook_capture())


def cmd_install_hook(args):
    """Explicit, transparent hook install — prints exactly what it writes."""
    from .install import HOOK_BLOCK, claude_settings_path, hook_present, install_claude_hook
    settings = Path(args.settings).expanduser() if args.settings \
        else claude_settings_path(globally=args.globally)
    data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    if hook_present(data):
        print("hook already installed in", settings)
        return
    scope = "every Claude Code project" if args.globally else "this project"
    print(f"Will add this UserPromptSubmit hook to {settings} ({scope}):\n")
    print(json.dumps(HOOK_BLOCK, indent=2))
    if not args.yes and input("\nProceed? [y/N] ").strip().lower() != "y":
        print("aborted")
        return
    install_claude_hook(settings)
    print(f"✓ installed. CRUX now injects context on every prompt in {scope}.")


def cmd_setup(args):
    from .setup_wizard import run
    run(non_interactive=args.yes,
        anthropic_key=args.anthropic_key or os.environ.get("ANTHROPIC_API_KEY"),
        openai_key=args.openai_key or os.environ.get("OPENAI_API_KEY"))


def cmd_ctx(args):
    """Print the context block for a task — paste into any tool without the hook."""
    from .hooks import _format
    store = _store()
    results, links = store.retrieve(args.task, limit=args.limit)
    if results:
        ids = [r.item.id for r in results] + [it.id for _, it in links]
        store.record_usage(ids, args.task)
        print(_format(results, links))
    else:
        print("(no relevant context yet)")
    store.close()


def cmd_serve(args):
    from .server import run
    run(Config.load())


def cmd_mcp(args):
    from .mcp_server import run
    run()


def cmd_app(args):
    from .app import run
    run(open_dashboard=not args.no_open)


def cmd_popup(args):
    from .quickcapture import run_standalone
    print(run_standalone(Config.load()))


def cmd_keytest(args):
    """Diagnose the global hotkey: prints every key pynput receives, and fires
    when your configured chord is detected. If NOTHING prints as you type, the OS
    is blocking low-level keyboard hooks (e.g. corporate security) — that's why
    the hotkey can't work, and we'll need a different capture trigger."""
    try:
        from pynput import keyboard
    except Exception as e:
        raise SystemExit(f"pynput not installed ({e}). Run: pip install 'crux[app]'")
    from .hotkey import chord_label, pynput_hotkey
    cfg = Config.load()
    hk = pynput_hotkey(cfg.hotkey_mods, cfg.hotkey_key)
    chord = chord_label(cfg.hotkey_mods, cfg.hotkey_key)
    print(f"\nKey test. Your chord is {chord}  ({hk}).")
    print("Type some keys, then try your chord. Ctrl+C to stop.\n")

    fired = {"n": 0}
    def on_chord():
        fired["n"] += 1
        print(f"  ✅ CHORD DETECTED  (#{fired['n']}) — the hotkey works!", flush=True)

    g = keyboard.GlobalHotKeys({hk: on_chord})
    g.start()

    def on_press(key):
        try:
            print(f"  key: {key}", flush=True)
        except Exception:
            pass
    with keyboard.Listener(on_press=on_press) as listener:
        try:
            listener.join()
        except KeyboardInterrupt:
            pass


def cmd_hotkey(args):
    from .hotkey import run
    run(install=args.install, out_dir=Config.load().home / "hotkey")


def _hook_installed(settings: str = ".claude/settings.json") -> bool:
    p = Path(settings)
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return "crux hook-inject" in json.dumps(data)
    except Exception:
        return False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="crux", description="Local context layer for AI coding agents.")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="capture a note, or ingest a document with --file")
    a.add_argument("text", nargs="?", default="")
    a.add_argument("--file", help="ingest any document (md/txt/...) → many facts")
    a.add_argument("--source"); a.add_argument("--type")
    a.add_argument("--main", action="store_true", help="capture straight into the verified main graph")
    a.set_defaults(func=cmd_add)

    cap = sub.add_parser("capture", help="grab the clipboard (bind to a global hotkey)")
    cap.set_defaults(func=cmd_capture)

    q = sub.add_parser("query", help="hybrid search")
    q.add_argument("query"); q.add_argument("--limit", type=int, default=5)
    q.add_argument("--scope", choices=["all", "main", "individual"], default="all")
    q.set_defaults(func=cmd_query)

    l = sub.add_parser("list", help="list items")
    l.add_argument("--type")
    l.add_argument("--scope", choices=["all", "main", "individual"], default="all")
    l.add_argument("--archived", action="store_true")
    l.set_defaults(func=cmd_list)

    pr = sub.add_parser("promote", help="promote a working item into the verified main graph")
    pr.add_argument("id")
    pr.add_argument("--title"); pr.add_argument("--summary"); pr.add_argument("--type")
    pr.set_defaults(func=cmd_promote)

    ed = sub.add_parser("edit", help="edit a fact's text (re-embeds + rechecks conflicts)")
    ed.add_argument("id"); ed.add_argument("--title"); ed.add_argument("--summary"); ed.add_argument("--type")
    ed.set_defaults(func=cmd_edit)

    de = sub.add_parser("demote", help="move a main item back to the working layer")
    de.add_argument("id"); de.set_defaults(func=cmd_demote)

    ar = sub.add_parser("archive"); ar.add_argument("id"); ar.add_argument("--restore", action="store_true")
    ar.set_defaults(func=cmd_archive)

    su = sub.add_parser("supersede"); su.add_argument("old"); su.add_argument("new")
    su.set_defaults(func=cmd_supersede)

    sub.add_parser("conflicts", help="list open contradiction candidates + how to resolve them").set_defaults(func=cmd_conflicts)

    di = sub.add_parser("dismiss", help="dismiss a conflict by id (won't resurface)")
    di.add_argument("id", type=int); di.set_defaults(func=cmd_dismiss)

    sub.add_parser("status").set_defaults(func=cmd_status)

    se = sub.add_parser("setup", help="one-command guided setup (keys, hook, hotkey)")
    se.add_argument("--yes", "-y", action="store_true",
                    help="non-interactive: apply recommended defaults, never prompt (for installers)")
    se.add_argument("--anthropic-key", default=None, help="set Anthropic key without prompting")
    se.add_argument("--openai-key", default=None, help="set OpenAI key without prompting")
    se.set_defaults(func=cmd_setup)

    cx = sub.add_parser("ctx", help="print relevant context for a task (paste into any tool)")
    cx.add_argument("task"); cx.add_argument("--limit", type=int, default=5)
    cx.set_defaults(func=cmd_ctx)

    ih = sub.add_parser("install-hook", help="install the UserPromptSubmit hook")
    ih.add_argument("--global", dest="globally", action="store_true",
                    help="install for every Claude Code project (~/.claude/settings.json)")
    ih.add_argument("--settings", default=None, help="explicit settings.json path")
    ih.add_argument("--yes", action="store_true")
    ih.set_defaults(func=cmd_install_hook)

    hk = sub.add_parser("hotkey", help="set up a global capture hotkey (writes platform snippets)")
    hk.add_argument("--install", action="store_true", help="write snippet files to ~/.crux/hotkey/")
    hk.set_defaults(func=cmd_hotkey)

    sub.add_parser("hook-inject").set_defaults(func=cmd_hook_inject)
    sub.add_parser("hook-capture").set_defaults(func=cmd_hook_capture)
    sub.add_parser("serve").set_defaults(func=cmd_serve)
    sub.add_parser("mcp").set_defaults(func=cmd_mcp)

    ap = sub.add_parser("app", help="run the tray/menubar app (owns the global hotkey)")
    ap.add_argument("--no-open", action="store_true", help="don't auto-open the dashboard")
    ap.set_defaults(func=cmd_app)

    sub.add_parser("popup", help="open the visible quick-capture box once (test the capture UI)").set_defaults(func=cmd_popup)
    sub.add_parser("keytest", help="diagnose the global hotkey: print keys pynput sees + detect your chord").set_defaults(func=cmd_keytest)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
