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


def run(*, non_interactive: bool = False, anthropic_key: str | None = None,
        openai_key: str | None = None, install_hook: bool = True,
        write_hotkey: bool = True) -> None:
    """Configure CRUX. In --yes/non-interactive mode it never prompts: it uses
    the keys passed in (or leaves you on offline models) and applies the
    recommended defaults, so an installer script can run it unattended."""
    cfg = Config.load()
    cfg.ensure_home()
    print("\n  CRUX setup — local-first, your data stays on this machine.\n")
    print(f"  Storing config in {cfg.home}\n")

    # 1. API keys (optional — everything works offline without them, just rougher)
    print("1) API keys make titles, search, and contradiction-checks much sharper.")
    if non_interactive:
        ak, ok = (anthropic_key or "").strip(), (openai_key or "").strip()
    else:
        print("   Press Enter to skip any (CRUX still works offline).")
        ak = input("   Anthropic API key: ").strip()
        ok = input("   OpenAI API key (for embeddings): ").strip()
    vals: dict[str, str] = {}
    if ak:
        vals["ANTHROPIC_API_KEY"] = ak
        vals["CRUX_PROCESSING_PROVIDER"] = "anthropic"
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
    if install_hook if non_interactive else _ask("   Install the hook globally (recommended)?"):
        status = install_claude_hook(claude_settings_path(globally=True))
        print("   ✓ " + ("already installed" if status == "already-installed"
                          else "installed in ~/.claude/settings.json") + "\n")
    else:
        print("   · skipped — run `crux install-hook` inside a project later\n")

    # 3. Capture hotkey snippets
    print("3) Global capture hotkey (select text anywhere → capture).")
    if write_hotkey if non_interactive else _ask("   Write the hotkey snippets for your OS?"):
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
