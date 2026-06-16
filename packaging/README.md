# Packaging CRUX as a downloadable desktop app

Turns CRUX into a double-clickable app (no Python required for end users) using
PyInstaller. Build on the OS you're targeting — PyInstaller does **not**
cross-compile, so build the macOS app on a Mac, the Windows exe on Windows.

## Build
```bash
bash packaging/build.sh
```
Outputs to `packaging/dist/`:
- **macOS** → `CRUX.app` — a menubar agent (no Dock icon). Drag to `/Applications`.
- **Windows** → `CRUX/CRUX.exe` — ship the whole `CRUX` folder (or wrap in an installer).
- **Linux** → `CRUX/CRUX` binary.

## First-run notes
- **macOS Accessibility:** the global hotkey + auto-copy need
  System Settings → Privacy & Security → Accessibility → enable CRUX. The app
  prompts the OS on first hotkey use.
- **Gatekeeper / SmartScreen:** unsigned apps get a warning. For real distribution:
  - macOS: sign with a Developer ID and **notarize** (`codesign` + `notarytool`).
  - Windows: sign with an Authenticode certificate.
- The app stores all data in `~/.crux/` exactly like the CLI — same database.

## What's bundled
`entry.py` launches `crux.app.run()` (tray icon + global hotkey + background
dashboard). `crux.spec` collects the dashboard SPA (`crux/static/`) and the
dynamic submodules of uvicorn/fastapi/pystray/pynput (and anthropic/openai if
installed at build time).

## Not yet done (real-launch checklist)
- [ ] An app icon (`.icns` / `.ico`) wired into the spec.
- [ ] Code signing + notarization (macOS) / Authenticode (Windows).
- [ ] An installer wrapper (DMG for mac, Inno Setup/MSI for Windows).
- [ ] CI (GitHub Actions matrix: macos + windows) to produce signed artifacts on tag.
