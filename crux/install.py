"""Shared installer helpers (used by `crux install-hook` and `crux setup`)."""

from __future__ import annotations

import json
from pathlib import Path

HOOK_BLOCK = {
    "matcher": "",
    "statusMessage": "Loading CRUX context...",
    "hooks": [{"type": "command", "command": "crux hook-inject"}],
}


def claude_settings_path(globally: bool) -> Path:
    """Global = every Claude Code session; otherwise this project only."""
    return (Path.home() / ".claude" / "settings.json") if globally \
        else Path(".claude/settings.json")


def hook_present(data: dict) -> bool:
    return "crux hook-inject" in json.dumps(data or {})


def install_claude_hook(settings: Path) -> str:
    """Idempotently add the UserPromptSubmit hook. Returns a status string."""
    data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    if hook_present(data):
        return "already-installed"
    hooks = data.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    hooks.append(HOOK_BLOCK)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return "installed"
