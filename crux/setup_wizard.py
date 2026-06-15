"""`crux setup` — one interactive command to configure everything: API keys
(persisted locally), the global Claude Code hook, and capture hotkey snippets.
Turns the 6-step manual setup into a single guided flow."""

from __future__ import annotations

from .config import Config, save_env_file
from .install import claude_settings_path, install_claude_hook


def _ask(prompt: str, default_yes: bool = True) -> bool:
    suffix = "[Y/n]" if default_yes else "[y/N]"
    ans = input(f"{prompt} {suffix} ").strip().lower()
    if not ans:
        return default_yes
    return ans.startswith("y")


def run() -> None:
    cfg = Config.load()
    cfg.ensure_home()
    print("\n  CRUX setup — local-first, your data stays on this machine.\n")
    print(f"  Storing config in {cfg.home}\n")

    # 1. API keys (optional — everything works offline without them, just rougher)
    print("1) API keys make titles, search, and contradiction-checks much sharper.")
    print("   Press Enter to skip any (CRUX still works offline).")
    vals: dict[str, str] = {}
    ak = input("   Anthropic API key: ").strip()
    if ak:
        vals["ANTHROPIC_API_KEY"] = ak
        vals["CRUX_PROCESSING_PROVIDER"] = "anthropic"
    ok = input("   OpenAI API key (for embeddings): ").strip()
    if ok:
        vals["OPENAI_API_KEY"] = ok
        vals["CRUX_EMBEDDING_PROVIDER"] = "openai"
    if vals:
        path = save_env_file(cfg.home, vals)
        print(f"   ✓ saved to {path}\n")
    else:
        print("   · skipped — running on offline models\n")

    # 2. Claude Code hook (global = works in every project automatically)
    print("2) Auto-inject context into Claude Code on every prompt.")
    if _ask("   Install the hook globally (recommended)?"):
        status = install_claude_hook(claude_settings_path(globally=True))
        print("   ✓ " + ("already installed" if status == "already-installed"
                          else "installed in ~/.claude/settings.json") + "\n")
    else:
        print("   · skipped — run `crux install-hook` inside a project later\n")

    # 3. Capture hotkey snippets
    print("3) Global capture hotkey (select text anywhere → capture).")
    if _ask("   Write the hotkey snippets for your OS?"):
        from .hotkey import run as hotkey_run
        print()
        hotkey_run(install=True, out_dir=cfg.home / "hotkey")
        print()
    else:
        print("   · skipped — run `crux hotkey --install` later\n")

    print("  Done. Next:")
    print("    • crux serve        → open the dashboard (http://127.0.0.1:7432)")
    print("    • restart Claude Code so the hook loads")
    print("    • crux add \"...\"   or your hotkey to capture\n")
