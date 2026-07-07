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

    # 2. Claude Code hooks (global = works in every project automatically)
    print("2) Auto resume, inject context, and capture work in Claude Code.")
    if install_hook if non_interactive else _ask("   Install the hooks globally (recommended)?"):
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

    # 4. Taskbar capture icon (copy text → click → pick a tag)
    print("4) Taskbar icon for one-click capture (copy text anywhere → click → tag).")
    if True if non_interactive else _ask("   Add the “Capture to CRUX” icon now?"):
        from .launcher import install_launcher
        res = install_launcher(cfg.home)
        print("   " + ("✓ " if res.get("ok") else "· ") + res.get("message", "") + "\n")
    else:
        print("   · skipped — add it later from the dashboard or `crux install-launcher`\n")

    cfg.mark_bootstrapped()   # launcher + hook + hotkey done → bare `crux` won't redo it
    cfg.mark_configured()     # setup finished → the dashboard opens straight in, not /setup

    print("  Done. Just run:")
    print("    • crux              → opens the dashboard; the capture icon is installed")
    print("    • restart Claude Code so the hook loads\n")
