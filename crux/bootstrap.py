"""First-run bootstrap — the "install once, everything just works" step.

The user should never run a string of setup commands. The first time CRUX
launches it wires up the machine automatically: the taskbar capture icon, the
Claude Code hooks, and the capture-hotkey snippets. It's idempotent and
best-effort — anything that fails is skipped, never fatal — and it runs exactly
once (guarded by the .bootstrapped marker), so normal launches stay instant.
"""

from __future__ import annotations

import sys

from .config import Config


def run_first_run(cfg: Config, *, force: bool = False, quiet: bool = False) -> list[str]:
    """Do the one-time machine setup. Returns a list of human-readable lines about
    what happened (for the launch banner). No-op after the first successful run
    unless force=True."""
    if cfg.is_bootstrapped() and not force:
        return []
    cfg.ensure_home()
    done: list[str] = []

    # 1. Taskbar capture icon — copy text anywhere → click → pick a tag.
    try:
        from .launcher import install_launcher
        res = install_launcher(cfg.home)
        done.append("✓ taskbar capture icon added" if res.get("ok")
                    else "· taskbar icon skipped (" + (res.get("message", "")[:40]) + ")")
    except Exception as e:
        done.append(f"· taskbar icon skipped ({str(e)[:40]})")

    # 2. Claude Code hooks (global) — auto resume / inject / capture. Only touches
    #    ~/.claude when Claude Code is present; harmless and idempotent otherwise.
    try:
        from .install import claude_settings_path, install_claude_hook
        status = install_claude_hook(claude_settings_path(globally=True))
        done.append("✓ Claude Code hooks installed" if status != "already-installed"
                    else "✓ Claude Code hooks already set")
    except Exception as e:
        done.append(f"· Claude hooks skipped ({str(e)[:40]})")

    # 3. Capture-hotkey snippets for this OS (for users who prefer a key chord).
    try:
        from .hotkey import write_snippets
        write_snippets(cfg.home / "hotkey", cfg.hotkey_mods, cfg.hotkey_key)
        done.append("✓ capture hotkey snippets written")
    except Exception:
        pass

    cfg.mark_bootstrapped()
    if not quiet and done:
        print("\n[crux] First-run setup (one time only):", file=sys.stderr)
        for line in done:
            print(f"        {line}", file=sys.stderr)
        print(file=sys.stderr)
    return done
