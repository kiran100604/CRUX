"""Shared installer helpers (used by `crux install-hook` and `crux setup`).

CRUX rides Claude Code's hook lifecycle so it works with zero extra steps:
  • SessionStart    → inject the resume brief (pick up where you left off)
  • UserPromptSubmit → inject context relevant to each prompt
  • Stop            → capture the turn's decisions/results into working memory
The install is explicit and prints exactly what it writes — no silent hooks.
"""

from __future__ import annotations

import json
from pathlib import Path

# (event, command, status message) for each hook CRUX installs.
HOOKS = [
    ("SessionStart", "crux hook-session-start", "Resuming with CRUX..."),
    ("UserPromptSubmit", "crux hook-inject", "Loading CRUX context..."),
    ("Stop", "crux hook-capture", "Saving to CRUX working memory..."),
]

# kept for back-compat with older callers that referenced the single inject block
HOOK_BLOCK = {
    "matcher": "",
    "statusMessage": "Loading CRUX context...",
    "hooks": [{"type": "command", "command": "crux hook-inject"}],
}


def _block(cmd: str, msg: str) -> dict:
    return {"matcher": "", "statusMessage": msg,
            "hooks": [{"type": "command", "command": cmd}]}


def blocks_preview() -> dict:
    """The exact JSON `install_claude_hooks` will add, keyed by event — so the CLI
    can show the user precisely what it's writing before it writes it."""
    return {event: _block(cmd, msg) for event, cmd, msg in HOOKS}


def claude_settings_path(globally: bool) -> Path:
    """Global = every Claude Code session; otherwise this project only."""
    return (Path.home() / ".claude" / "settings.json") if globally \
        else Path(".claude/settings.json")


def hook_present(data: dict) -> bool:
    return "crux hook-inject" in json.dumps(data or {})


def install_claude_hooks(settings: Path) -> dict:
    """Idempotently add all of CRUX's hooks. Returns {event: installed|already}."""
    data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    status: dict[str, str] = {}
    changed = False
    for event, cmd, msg in HOOKS:
        arr = data.setdefault("hooks", {}).setdefault(event, [])
        if any(cmd in json.dumps(h) for h in arr):
            status[event] = "already-installed"
        else:
            arr.append(_block(cmd, msg))
            status[event] = "installed"
            changed = True
    if changed:
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return status


def install_claude_hook(settings: Path) -> str:
    """Back-compat shim: install the full hook set, return a single status string."""
    res = install_claude_hooks(settings)
    return "installed" if any(v == "installed" for v in res.values()) else "already-installed"
