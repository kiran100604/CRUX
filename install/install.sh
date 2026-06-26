#!/usr/bin/env bash
# CRUX one-line installer for macOS / Linux.
#   curl -fsSL https://raw.githubusercontent.com/kiran100604/crux/claude/pensive-bell-jj7bqr/install/install.sh | bash
# Installs CRUX (with the tray app), prefers pipx so `crux` lands on PATH, and
# runs setup with offline defaults. Export CRUX_ANTHROPIC_KEY / CRUX_OPENAI_KEY
# beforehand to wire keys non-interactively.
set -euo pipefail

REPO="${CRUX_REPO:-https://github.com/kiran100604/crux.git}"
BRANCH="${CRUX_BRANCH:-claude/pensive-bell-jj7bqr}"
SPEC="git+${REPO}@${BRANCH}#egg=crux[app]"

printf '\n  Installing CRUX...\n'

# 1. Need Python 3.11+.
PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || { echo "Python 3.11+ not found. Install it, then re-run."; exit 1; }

# 2. Prefer pipx (isolated + auto-PATH); fall back to pip --user.
if command -v pipx >/dev/null 2>&1; then
    echo "  Installing via pipx..."
    pipx install --force "$SPEC"
    pipx ensurepath >/dev/null 2>&1 || true
    RUN="crux"
else
    echo "  pipx not found; installing via pip --user..."
    "$PY" -m pip install --user --upgrade "$SPEC"
    RUN="$PY -m crux.cli"
fi

# 3. Configure non-interactively.
SETUP_ARGS=(setup --yes)
[ -n "${CRUX_ANTHROPIC_KEY:-}" ] && SETUP_ARGS+=(--anthropic-key "$CRUX_ANTHROPIC_KEY")
[ -n "${CRUX_OPENAI_KEY:-}" ]    && SETUP_ARGS+=(--openai-key "$CRUX_OPENAI_KEY")
$RUN "${SETUP_ARGS[@]}"

# 4. Launch CRUX now — nothing else for the user to run. The dashboard opens and
#    the "Capture to CRUX" icon lands in the app menu (pin it).
printf '\n  CRUX is ready — launching...\n'
nohup $RUN start >/dev/null 2>&1 &
echo "  The dashboard is opening at http://127.0.0.1:7432."
echo "  'Capture to CRUX' is in your app menu — pin it to your taskbar/dock."
echo "  Restart Claude Code so the context hook loads."
printf '\n'
