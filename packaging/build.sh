#!/usr/bin/env bash
# Build the CRUX desktop app for the CURRENT OS.
# Run on macOS to get CRUX.app, on Windows (Git Bash) to get CRUX.exe, on Linux for a binary.
set -euo pipefail
cd "$(dirname "$0")"

echo "→ installing build deps (app extras + pyinstaller)…"
python -m pip install -e "..[app]" pyinstaller >/dev/null

echo "→ building with PyInstaller…"
rm -rf build dist
pyinstaller crux.spec --noconfirm

echo
echo "✓ Done. Artifacts in packaging/dist/:"
ls -1 dist
case "$(uname -s)" in
  Darwin) echo "  → dist/CRUX.app  (drag to /Applications; first run: allow Accessibility)";;
  *)      echo "  → dist/CRUX/     (zip and ship the whole folder)";;
esac
