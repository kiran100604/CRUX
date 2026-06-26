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
        item = store.capture(content, source=args.source, type_hint=args.type, scope=scope,
                             owner=Config.load().user)
        print(f"✓ captured: {item.title}  (id={item.id[:8]}, type={item.type}, scope={item.scope})")
    store.close()


def _feedback(msg: str, ok: bool = True) -> None:
    """Visible confirmation for a bound-shortcut capture: prefer CRUX's own flash
    toast (the dark bottom-center one), fall back to a system notification."""
    try:
        from .quickcapture import flash_standalone
        if flash_standalone(msg, ok=ok):
            return
    except Exception:
        pass
    import shutil
    import subprocess
    try:
        if sys.platform == "darwin":
            subprocess.run(["osascript", "-e",
                            f'display notification "{msg}" with title "CRUX"'], timeout=3)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", "-t", "2500", "CRUX", msg], timeout=3)
    except Exception:
        pass


def cmd_capture(args):
    """Hotkey entrypoint: grab the clipboard and capture it. Bind a global
    shortcut to `crux capture` (see README for Raycast/Hammerspoon snippets)."""
    text = read_clipboard()
    if not text or not text.strip():
        print("clipboard is empty — nothing to capture")
        _feedback("Clipboard empty — copy text first", ok=False)
        return
    cfg = Config.load()
    # Hotkey capture lands in WORKING memory as a raw step on the current thread —
    # kept as narrative ("what I'm doing"), never atomized into facts up front.
    if cfg.server:  # team mode: send to the shared graph's working layer
        from . import client
        try:
            client.capture(cfg.server, text, source="hotkey", user=cfg.user, as_step=True)
        except Exception as e:
            print(f"error: shared server unreachable ({e})")
            _feedback("Capture failed — server unreachable", ok=False)
            return
        msg = f"Added to working memory: {text.strip()[:60]}"
        print("✓ " + msg)
        _feedback(msg, ok=True)
        return
    store = _store()
    res = store.add_step(text, source="hotkey")
    where = "current thread" if res.get("thread_id") else "working memory"
    store.close()
    msg = f"Added to {where}: {text.strip()[:60]}"
    print("✓ " + msg)
    _feedback(msg, ok=True)  # blocks ~1.5s for the flash fade, then exits


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
    if cfg.server:
        from . import client
        reachable = "reachable" if client.ping(cfg.server) else "UNREACHABLE"
        print(f"mode:       team — shared server {cfg.server} ({reachable})")
        print(f"user:       {cfg.user}")
    else:
        print(f"mode:       local (run `crux connect <url>` to join a team server)")
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


def cmd_hook_session_start(args):
    from .hooks import hook_session_start
    raise SystemExit(hook_session_start())


def cmd_install_hook(args):
    """Explicit, transparent hook install — prints exactly what it writes."""
    from .install import blocks_preview, claude_settings_path, install_claude_hooks
    settings = Path(args.settings).expanduser() if args.settings \
        else claude_settings_path(globally=args.globally)
    scope = "every Claude Code project" if args.globally else "this project"
    print(f"Will add CRUX's Claude Code hooks to {settings} ({scope}):\n")
    print("  • SessionStart    → resume where you left off")
    print("  • UserPromptSubmit → inject relevant context on every prompt")
    print("  • Stop            → capture decisions/results into working memory\n")
    print(json.dumps(blocks_preview(), indent=2))
    if not args.yes and input("\nProceed? [y/N] ").strip().lower() != "y":
        print("aborted")
        return
    res = install_claude_hooks(settings)
    print("✓ " + ", ".join(f"{k}: {v}" for k, v in res.items()))
    print(f"CRUX now captures and injects automatically in {scope} — "
          "open the dashboard only when you want to.")


def cmd_setup(args):
    from .setup_wizard import run
    run(non_interactive=args.yes,
        anthropic_key=args.anthropic_key or os.environ.get("ANTHROPIC_API_KEY"),
        openai_key=args.openai_key or os.environ.get("OPENAI_API_KEY"))


def _retrieve_brief(task, limit):
    """Return the directive brief for a task — from the shared server if connected,
    else the local graph."""
    cfg = Config.load()
    if cfg.server:
        from . import client
        return client.retrieve(cfg.server, task, user=cfg.user, limit=limit).get("context") or ""
    from .hooks import _format
    store = _store()
    try:
        results, links = store.retrieve(task, limit=limit, user=cfg.user)
        if not results:
            return ""
        ids = [r.item.id for r in results] + [it.id for _, it in links]
        store.record_usage(ids, task, user=cfg.user)
        return _format(results, links)
    finally:
        store.close()


def cmd_ctx(args):
    """Print the context block for a task — paste into any tool without the hook."""
    print(_retrieve_brief(args.task, args.limit) or "(no relevant context yet)")


def cmd_brief(args):
    """Print the current thread's portable brief — intent + working memory + open
    questions + the KB knowledge relevant to where things stand right now. Paste it
    into any AI tool, no re-explaining."""
    store = _store()
    try:
        tid = args.thread or store.current_thread_id()
        if not tid:
            print("(no active thread — start one in the dashboard's Working tab, "
                  "or capture something to begin)")
            return
        text = store.thread_brief(tid)
        print(text or "(thread has no context yet)")
    finally:
        store.close()


def cmd_enhance(args):
    """Enrich a prompt with the team's intent: prints a ready-to-paste prompt =
    your task + the directive brief (constraints, decisions, architecture, context)
    so the agent builds what's actually intended."""
    brief = _retrieve_brief(args.task, args.limit)
    print(f"TASK: {args.task}\n\n{brief or '(no relevant team context found — capture some first)'}")


def cmd_connect(args):
    """Point this machine at a shared CRUX server (team mode). After this, the
    hook and capture/enhance talk to the team's intent graph, not the local DB."""
    from . import client
    from .config import save_env_file
    cfg = Config.load()
    url = args.url.rstrip("/")
    vals = {"CRUX_SERVER": url}
    if args.user:
        vals["CRUX_USER"] = args.user
    if args.leader:
        vals["CRUX_ADMIN_TOKEN"] = args.leader
    save_env_file(cfg.home, vals)
    who = args.user or cfg.user
    role = "leader" if args.leader else "member (propose & use)"
    if client.ping(url):
        print(f"✓ Connected to {url} as {who} — {role}. Hook & capture now use the team graph.")
        if args.leader:
            print(f"  Validate the KB in the dashboard: {url}/?token={args.leader}")
    else:
        print(f"Saved {url} as {who}, but couldn't reach it — is `crux serve` running there?")


def cmd_doctor(args):
    """Diagnose capture in one shot: environment, the registered hotkey, the
    clipboard, and the exact command your OS shortcut runs."""
    import os
    import shutil
    from .hotkey import chord_label, gnome_binding
    cfg = Config.load()
    chord = chord_label(cfg.hotkey_mods, cfg.hotkey_key)
    print(f"platform: {sys.platform}")
    print(f"chord:    {chord}")
    if sys.platform == "linux":
        print(f"desktop:  {os.environ.get('XDG_CURRENT_DESKTOP','?')} · session {os.environ.get('XDG_SESSION_TYPE','?')}")
        print(f"gsettings: {'yes' if shutil.which('gsettings') else 'NO — not GNOME, bind manually'}")
        print(f"clipboard tools: wl-paste={'y' if shutil.which('wl-paste') else 'n'} xclip={'y' if shutil.which('xclip') else 'n'}")
        if shutil.which("gsettings"):
            import subprocess
            path = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/crux/"
            b = subprocess.run(["gsettings", "get", path, "binding"], capture_output=True, text=True).stdout.strip()
            c = subprocess.run(["gsettings", "get", path, "command"], capture_output=True, text=True).stdout.strip()
            print(f"registered binding: {b or '(none)'}  → expected {gnome_binding(cfg.hotkey_mods, cfg.hotkey_key)!r}")
            print(f"registered command: {c or '(none)'}")
    clip = read_clipboard()
    print(f"clipboard now: {repr(clip[:60]) if clip else '(empty)'}")
    print("\nIsolation test → copy some text, then run:  crux capture")
    print("  • if that captures, the binding is the only issue (re-bind / pick another chord)")
    print("  • if that fails, it's clipboard/setup (check the tools above)")


def cmd_start(args):
    """One command, everywhere: starts the dashboard AND makes the capture hotkey
    work using whatever the OS needs — no modes to think about.
      • Windows/macOS: in-app global hotkey + tray (+ dashboard).
      • Linux: registers an OS-level shortcut (works under Wayland) + dashboard.
    Capture is always: copy text (Ctrl+C) → press your chord.
    """
    cfg = Config.load()
    # First launch wires up the machine automatically (taskbar icon, hooks,
    # hotkey) — one time, no extra commands. Idempotent after that.
    from .bootstrap import run_first_run
    run_first_run(cfg)
    if sys.platform == "linux":
        import os
        import threading
        import webbrowser
        from .hotkey import bind_gnome, chord_label
        from .server import run as serve_run
        chord = chord_label(cfg.hotkey_mods, cfg.hotkey_key)
        cmd = f"{sys.executable} -m crux.cli capture"
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
        base = f"http://{cfg.host}:{cfg.port}"
        url = base if cfg.is_configured() else base + "/setup"
        print(f"CRUX is running at {base}")
        ok = False
        if "GNOME" in desktop.upper():
            ok, info = bind_gnome(cfg.hotkey_mods, cfg.hotkey_key, cmd)
        if ok:
            print(f"✓ Hotkey ready. Capture: copy text (Ctrl+C), then press {chord}.")
        else:
            why = f"your desktop ({desktop or 'unknown'}) manages its own shortcuts" \
                  if "GNOME" not in desktop.upper() else f"auto-bind failed ({info})"
            print(f"⚠ One-time setup — {why}, so bind the hotkey yourself:")
            print(f"    Settings → Keyboard → Custom Shortcuts → +")
            print(f"      Command:  {cmd}")
            print(f"      Shortcut: {chord}")
            print(f"  Then: copy text (Ctrl+C) → press {chord}. (Capture itself already works: `crux capture`.)")
        if not args.no_open:
            # Open the browser only once the server is actually accepting
            # connections — a fixed delay opens a blank tab before boot finishes
            # on slower machines, which looks like a long load.
            import socket
            import time
            def _open_when_ready():
                for _ in range(80):  # up to ~20s
                    try:
                        with socket.create_connection((cfg.host, cfg.port), timeout=0.5):
                            break
                    except OSError:
                        time.sleep(0.25)
                webbrowser.open(url)
            threading.Thread(target=_open_when_ready, daemon=True).start()
        serve_run(cfg)
    else:
        from .app import run as app_run
        app_run(open_dashboard=not args.no_open)


def cmd_serve(args):
    from .server import _admin_token, run
    from .bootstrap import run_first_run
    cfg = Config.load()
    run_first_run(cfg)                    # one-time machine setup on first launch
    tok = _admin_token(cfg)
    print(f"CRUX server on http://{cfg.host}:{cfg.port}  (you = leader on this machine)")
    print("Team setup:")
    print(f"  • Members:        crux connect http://<this-host>:{cfg.port} --user <name>")
    print(f"  • Leader (remote): open  http://<this-host>:{cfg.port}/?token={tok}")
    if sys.platform == "linux":
        print("Capture hotkey: run `crux bind` once (registers an OS shortcut).")
    else:
        print("Capture hotkey: this is dashboard-only — run `crux app` instead "
              "(dashboard + hotkey + tray).")
    run(cfg)


def cmd_mcp(args):
    from .mcp_server import run
    run()


def cmd_install_mcp(args):
    """Register CRUX as an MCP server in Claude Code, so the agent can pull context
    (get_context) and log decisions (log_work) into your CRUX. One command."""
    import shutil
    import subprocess
    py = sys.executable
    try:
        import mcp  # noqa: F401
    except Exception:
        print("⚠ MCP SDK missing — run:  py -m pip install 'crux[mcp]'  (then re-run this)")
    if shutil.which("claude"):
        try:
            r = subprocess.run(["claude", "mcp", "add", "crux", "-s", "user",
                                "--", py, "-m", "crux.cli", "mcp"],
                               capture_output=True, text=True)
        except Exception as e:
            r = subprocess.CompletedProcess([], 1, "", str(e))
        if r.returncode == 0:
            print("✓ Registered CRUX with Claude Code (user scope).")
            print("  Restart Claude Code, then run /mcp — you should see 'crux'.")
            print("  The agent can now call get_context() and log_work() against your CRUX.")
            return
        print("  (`claude mcp add` failed: " + (r.stderr or r.stdout).strip()[:160] + ")")
    # fallback: project-scoped .mcp.json in the current folder
    import json
    p = Path(".mcp.json")
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.setdefault("mcpServers", {})["crux"] = {"command": py, "args": ["-m", "crux.cli", "mcp"]}
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"✓ Wrote {p.resolve()}")
    print("  Open Claude Code in this folder and approve the 'crux' server (/mcp).")


# CRUX documenting itself — a structured doc so each section heading becomes the
# fact's SUBJECT (what it's about), exercising the real ingest → subject pipeline.
_SEED_DOC = """# What is CRUX
CRUX is a local-first context layer for AI coding agents. It captures what you
decide and learn, then injects the relevant pieces into Claude Code and Cursor
automatically, so you stop re-explaining context and agents stop contradicting
decisions you already made.

# Users
The primary users are individual developers working with AI coding agents. CRUX
also supports small software teams, where a leader verifies knowledge and members
propose and use it.

# Problem
AI agents are amnesiac — they forget every decision the moment a session ends.
Developers waste hours re-explaining context, and agents repeat mistakes the team
already ruled out. The bottleneck is memory, not model quality.

# Competition
Hyper is a cloud "company brain" that ingests Slack, Gmail and Docs. Glean is
enterprise search. CRUX differs by being local-first, individual-developer first,
and intent-aware; your data never leaves your machine.

# Architecture
CRUX uses a two-tier memory: a working layer for transient proposals and a main
graph of verified truth. Everything is stored first as an Episode (the raw source
of truth); atomic facts are extracted from episodes and link back to them.

# Trust gate
Captured facts start in the working layer as proposals. A human promotes the
durable ones into the verified main graph — nothing reaches agents as trusted
until it is promoted. Newer decisions supersede older ones without deleting history.

# Storage
All data lives in one local SQLite file. Each fact's embedding is stored as a BLOB
on the row, and an FTS5 virtual table mirrors the facts for keyword search. There
is no separate vector database to keep in sync.

# Retrieval
Retrieval fuses semantic vector search with FTS keyword search using reciprocal
rank fusion, then re-ranks by trust, type, recency and subject. A subject boost
ranks on-subject facts above cross-subject noise.

# Subjects
Every fact is tagged with a subject — the specific thing it is about, such as a
service, feature or area. The knowledge base is browsed by subject, and retrieval
scopes to the relevant subject so unrelated facts do not leak in.

# Capture and hooks
Capture happens through a global hotkey and through Claude Code lifecycle hooks.
The SessionStart hook injects a resume brief, every prompt pulls relevant context,
and the Stop hook captures the turn's decisions into working memory automatically.

# Working memory and sessions
A thread is a project: a stable intent plus an evolving working memory of
decisions, state and open questions. Captures group into sessions, and reopening a
project resumes from the last checkpoint.

# Privacy
CRUX is local-first and runs fully offline by default. Your data never leaves your
machine; API keys are optional and only sharpen enrichment and synthesis.
"""


def cmd_seed_demo(args):
    """Document CRUX inside CRUX: ingest its own product knowledge as a structured
    doc (section headings → subjects) and promote it into the verified graph."""
    store = _store()
    res = store.ingest(_SEED_DOC, source_type="file", source_ref="crux-docs.md",
                       title="CRUX", scope="individual")
    facts = res.get("facts", [])
    for f in facts:
        store.promote(f.id)
    from collections import Counter
    groups = Counter((f.subject or "general") for f in facts)
    print(f"  ingested + verified {len(facts)} facts across {len(groups)} subjects:")
    for subj, n in groups.most_common():
        print(f"    {n:2d}  {subj}")
    store.close()
    print("\nDone. Run `crux serve` → open http://127.0.0.1:7432 → Knowledge Base (By subject).")


def cmd_use_nvidia(args):
    """Point CRUX at NVIDIA NIM's free OpenAI-compatible API (build.nvidia.com).
    Enables real enrichment/tier/domain/conflict judgment with a free key."""
    from .config import save_env_file
    cfg = Config.load()
    vals = {
        "CRUX_PROCESSING_PROVIDER": "openai",
        "OPENAI_API_KEY": args.key.strip(),
        "CRUX_OPENAI_BASE_URL": "https://integrate.api.nvidia.com/v1",
        "CRUX_PROCESSING_MODEL": args.model,
    }
    if args.embeddings:
        vals["CRUX_EMBEDDING_PROVIDER"] = "openai"
        vals["CRUX_EMBEDDING_MODEL"] = args.embed_model
    save_env_file(cfg.home, vals)
    print(f"✓ CRUX now uses NVIDIA NIM for enrichment (model: {args.model}).")
    if args.embeddings:
        print(f"  Embeddings: {args.embed_model}.")
        # re-embed existing facts with the new provider so old/new vectors don't mix
        store = _store()
        existing = len(store.db.list(archived=False, limit=100000))
        if existing:
            print(f"  Re-embedding {existing} existing fact(s) with NVIDIA…")
            try:
                n = store.reembed_all()
                print(f"  ✓ re-embedded {n} fact(s).")
            except Exception as e:
                print(f"  ⚠ re-embed failed ({e}). Run `crux reembed` once the key/network is reachable.")
        store.close()
    else:
        print("  Embeddings stay offline (fine for testing). Add --embeddings to use NVIDIA's too.")
    print("  Test it:  crux add \"We chose Stripe over Razorpay for fees.\" --type decision  →  crux query stripe")


def cmd_reembed(args):
    """Recompute every fact's embedding with the current provider (run after
    switching embedders, e.g. offline→NVIDIA)."""
    store = _store()
    print(f"Re-embedding with {store.embedder.model}…")
    n = store.reembed_all()
    store.close()
    print(f"✓ re-embedded {n} fact(s).")


def cmd_tidy(args):
    """Archive private working memory older than --days (keeps the backlog clean;
    nominations and verified facts are never touched)."""
    store = _store()
    n = store.expire_working_memory(days=args.days)
    store.close()
    print(f"✓ archived {n} stale working-memory item(s) (older than {args.days}d)")


def cmd_export(args):
    """Export the KB to a JSON file (commit it to git so the KB travels with the repo)."""
    import json
    store = _store()
    items = store.export_items()
    store.close()
    Path(args.path).write_text(
        json.dumps({"version": 1, "items": items}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    print(f"✓ exported {len(items)} item(s) → {args.path}")


def cmd_import(args):
    """Import a KB JSON file into this machine's CRUX (idempotent — deduped by hash)."""
    import json
    p = Path(args.path)
    if not p.exists():
        print(f"file not found: {p}"); return
    data = json.loads(p.read_text(encoding="utf-8"))
    store = _store()
    n = store.import_items(data.get("items", []))
    store.close()
    print(f"✓ imported {n} new item(s) from {args.path}  (run `crux serve` to view)")




def cmd_app(args):
    from .app import run
    from .bootstrap import run_first_run
    run_first_run(Config.load())          # one-time machine setup on first launch
    run(open_dashboard=not args.no_open)


def cmd_popup(args):
    from .quickcapture import run_standalone
    print(run_standalone(Config.load()))


def cmd_install_launcher(args):
    """Add a taskbar/menu launcher for the capture popup. Click it after copying
    text → pick a tag. Same thing the dashboard's 'Add to taskbar' button does."""
    from .launcher import install_launcher
    res = install_launcher(Config.load().home)
    print(("✓ " if res["ok"] else "⚠ ") + res["message"])
    if res.get("path"):
        print(f"  ({res['path']})")


def cmd_bind(args):
    """Register a GNOME custom shortcut → `crux capture`. This is the reliable
    capture path on Wayland (the compositor grabs the key, regardless of which
    app is focused — unlike an in-app hotkey)."""
    from .hotkey import bind_gnome, chord_label
    cfg = Config.load()
    chord = chord_label(cfg.hotkey_mods, cfg.hotkey_key)
    command = f"{sys.executable} -m crux.cli capture"
    ok, info = bind_gnome(cfg.hotkey_mods, cfg.hotkey_key, command)
    if ok:
        print(f"✓ Bound {chord} → crux capture (GNOME).\n"
              f"  Now: select text anywhere, Ctrl+C, press {chord}.\n"
              f"  (Change it in Settings → Keyboard → Custom Shortcuts → 'Capture to CRUX'.)")
    else:
        print("Couldn't auto-bind (" + info + "). Bind manually:\n"
              "  Settings → Keyboard → Custom Shortcuts → +\n"
              f"  Command: {command}\n  Shortcut: {chord}")


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
    if sys.platform == "linux":
        sess = os.environ.get("XDG_SESSION_TYPE", "?")
        print(f"Session: {sess}" + ("  ⚠ Wayland blocks global key capture — if nothing prints "
              "below, bind `crux capture` via GNOME Settings instead." if sess == "wayland"
              or os.environ.get("WAYLAND_DISPLAY") else ""))
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
    p.add_argument("--no-open", action="store_true",
                   help="with bare `crux`: don't auto-open the dashboard")
    # No subcommand required: bare `crux` is the one command — first-run setup
    # (taskbar icon, hooks, hotkey) happens automatically, then the dashboard opens.
    sub = p.add_subparsers(dest="command", required=False)

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

    br = sub.add_parser("brief", help="print the current thread's portable context (paste anywhere)")
    br.add_argument("thread", nargs="?", default=None, help="thread id (defaults to the current thread)")
    br.set_defaults(func=cmd_brief)

    en = sub.add_parser("enhance", help="enrich a prompt with team intent (task + directive brief)")
    en.add_argument("task"); en.add_argument("--limit", type=int, default=6)
    en.set_defaults(func=cmd_enhance)

    cn = sub.add_parser("connect", help="connect to a shared CRUX server (team mode)")
    cn.add_argument("url", help="e.g. http://crux.internal:7432")
    cn.add_argument("--user", default=None, help="your name on the team graph")
    cn.add_argument("--leader", default=None, metavar="TOKEN",
                    help="admin token → validate the KB remotely (else member: propose & use)")
    cn.set_defaults(func=cmd_connect)

    ih = sub.add_parser("install-hook", help="install CRUX's Claude Code hooks (resume + inject + capture)")
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
    sub.add_parser("hook-session-start").set_defaults(func=cmd_hook_session_start)
    sp = sub.add_parser("start", help="same as bare `crux` — first-run setup + dashboard, every OS")
    sp.add_argument("--no-open", action="store_true", help="don't auto-open the dashboard")
    sp.set_defaults(func=cmd_start)
    sub.add_parser("serve").set_defaults(func=cmd_serve)
    sub.add_parser("doctor", help="diagnose why the capture hotkey isn't firing").set_defaults(func=cmd_doctor)
    sub.add_parser("mcp").set_defaults(func=cmd_mcp)
    sub.add_parser("install-mcp", help="register CRUX with Claude Code (agent can pull + log)").set_defaults(func=cmd_install_mcp)
    sub.add_parser("seed-demo", help="load CRUX's own product knowledge into the KB (cross-platform)").set_defaults(func=cmd_seed_demo)

    nv = sub.add_parser("use-nvidia", help="use NVIDIA NIM's free OpenAI-compatible API for enrichment")
    nv.add_argument("key", help="your NVIDIA API key from build.nvidia.com")
    nv.add_argument("--model", default="meta/llama-3.3-70b-instruct", help="chat model id")
    nv.add_argument("--embeddings", action="store_true", help="also use NVIDIA embeddings (changes vector size)")
    nv.add_argument("--embed-model", default="nvidia/nv-embedqa-e5-v5")
    nv.set_defaults(func=cmd_use_nvidia)

    sub.add_parser("reembed", help="recompute all embeddings with the current provider").set_defaults(func=cmd_reembed)

    td = sub.add_parser("tidy", help="archive stale private working memory (backlog hygiene)")
    td.add_argument("--days", type=int, default=7)
    td.set_defaults(func=cmd_tidy)

    ex = sub.add_parser("export", help="export the KB to JSON (commit it so the KB travels via git)")
    ex.add_argument("path", nargs="?", default="crux_kb.json")
    ex.set_defaults(func=cmd_export)

    im = sub.add_parser("import", help="import a KB JSON file into this machine (deduped)")
    im.add_argument("path", nargs="?", default="crux_kb.json")
    im.set_defaults(func=cmd_import)

    ap = sub.add_parser("app", help="run the tray/menubar app (owns the global hotkey)")
    ap.add_argument("--no-open", action="store_true", help="don't auto-open the dashboard")
    ap.set_defaults(func=cmd_app)

    sub.add_parser("popup", help="open the visible quick-capture box once (test the capture UI)").set_defaults(func=cmd_popup)
    sub.add_parser("install-launcher", help="add a taskbar/menu icon that opens the tag-capture popup").set_defaults(func=cmd_install_launcher)
    sub.add_parser("keytest", help="diagnose the global hotkey: print keys pynput sees + detect your chord").set_defaults(func=cmd_keytest)
    sub.add_parser("bind", help="register a GNOME shortcut → crux capture (reliable on Wayland)").set_defaults(func=cmd_bind)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not getattr(args, "command", None):
        # bare `crux` → the one command: first-run setup + dashboard.
        return cmd_start(args)
    args.func(args)


if __name__ == "__main__":
    main()
