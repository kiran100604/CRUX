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


# --------------------------------------------------------------------------- #
# Editor integration (Cursor / VS Code) via MCP.
#
# Claude Code gets the ambient hook path above. Every other agent connects
# through CRUX's MCP server (`crux mcp`), which exposes get_context / log_work /
# remember. These helpers write the right config for each editor so the user
# never hand-edits JSON, and (optionally) drop a small "rules" file that tells
# the agent to call those tools on its own — the closest thing to hooks that
# Cursor and VS Code offer.
# --------------------------------------------------------------------------- #

# Cursor reads project rules from .cursor/rules/*.mdc (MDC = markdown + frontmatter).
CURSOR_RULES = """\
---
description: Use CRUX shared project memory
alwaysApply: true
---
This project uses **CRUX** for shared, verified context, exposed as MCP tools.

- BEFORE starting any task, call the MCP tool `get_context` with a short
  description of what you're about to do. Treat returned constraints as hard
  rules, follow prior decisions, and flag anything in the request that conflicts.
- AFTER finishing a meaningful unit of work, call `log_work` with the durable
  decisions, constraints, and knowledge you produced. Log generously —
  everything is proposed for human review before it becomes trusted.
"""

# VS Code (Copilot agent mode) reads .github/copilot-instructions.md. We fence our
# block so re-running is idempotent and we never clobber the user's own guidance.
VSCODE_RULES = """\
<!-- CRUX:start -->
## CRUX shared project memory

This project uses **CRUX** for shared, verified context, exposed as MCP tools.

- BEFORE starting any task, call `get_context` with a short description of what
  you're about to do. Treat returned constraints as hard rules and follow prior
  decisions.
- AFTER finishing meaningful work, call `log_work` with the durable decisions,
  constraints, and knowledge you produced. Everything is proposed for human
  review before it becomes trusted.
<!-- CRUX:end -->
"""


def _mcp_spec(python_exe: str, crux_home: Path | None = None) -> dict:
    """The stdio command an editor runs to launch CRUX's MCP server. Carries
    CRUX_HOME only when it's non-default, so a custom home still resolves."""
    spec: dict = {"command": python_exe, "args": ["-m", "crux.cli", "mcp"]}
    default_home = (Path.home() / ".crux").resolve()
    if crux_home and crux_home.resolve() != default_home:
        spec["env"] = {"CRUX_HOME": str(crux_home)}
    return spec


def _merge_mcp(path: Path, key: str, spec: dict, wrap: dict | None = None) -> None:
    """Idempotently add the crux server under `key` (mcpServers / servers),
    preserving any other servers the user already configured."""
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.setdefault(key, {})["crux"] = {**(wrap or {}), **spec}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def connect_editor(editor: str, project_dir: str | None, python_exe: str,
                   write_rules: bool = True, crux_home: Path | None = None) -> dict:
    """Wire an editor up to CRUX's MCP server (+ optional auto-use rules file).
    Returns {ok, editor, written:[paths], notes:[...], next} for the UI to show."""
    editor = (editor or "").strip().lower()
    spec = _mcp_spec(python_exe, crux_home)
    written: list[str] = []
    notes: list[str] = []
    proj: Path | None = None
    if project_dir:
        proj = Path(project_dir).expanduser()
        if not proj.is_dir():
            return {"ok": False, "error": f"Project folder not found: {proj}"}

    if editor == "cursor":
        # Cursor's MCP config is global (~/.cursor/mcp.json) → CRUX is available in
        # every Cursor project without picking a folder. Rules are per-project.
        p = Path.home() / ".cursor" / "mcp.json"
        _merge_mcp(p, "mcpServers", spec)
        written.append(str(p))
        if write_rules:
            if proj:
                r = proj / ".cursor" / "rules" / "crux.mdc"
                r.parent.mkdir(parents=True, exist_ok=True)
                r.write_text(CURSOR_RULES, encoding="utf-8")
                written.append(str(r))
            else:
                notes.append("No project folder given — connected MCP globally but "
                             "skipped the auto-use rules file (it's per-project).")
        return {"ok": True, "editor": "Cursor", "written": written, "notes": notes,
                "next": "Restart Cursor, then check Settings → MCP for 'crux'."}

    if editor in ("vscode", "vs code", "code"):
        # VS Code's MCP config is per-workspace (.vscode/mcp.json, `servers` key).
        if not proj:
            return {"ok": False,
                    "error": "VS Code needs a project folder — its MCP config is per-workspace."}
        p = proj / ".vscode" / "mcp.json"
        _merge_mcp(p, "servers", spec, wrap={"type": "stdio"})
        written.append(str(p))
        if write_rules:
            ci = proj / ".github" / "copilot-instructions.md"
            ci.parent.mkdir(parents=True, exist_ok=True)
            existing = ci.read_text(encoding="utf-8") if ci.exists() else ""
            if "CRUX:start" not in existing:
                sep = "\n\n" if existing.strip() else ""
                ci.write_text(existing + sep + VSCODE_RULES, encoding="utf-8")
            written.append(str(ci))
        return {"ok": True, "editor": "VS Code", "written": written, "notes": notes,
                "next": "In VS Code: Command Palette → 'MCP: List Servers' → start 'crux' (agent mode)."}

    return {"ok": False, "error": f"Unknown editor: {editor!r}"}
