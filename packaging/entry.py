"""Entry point for the packaged CRUX desktop app (PyInstaller bundles this).
Launches the tray app, which owns the global hotkey and serves the dashboard."""

from crux.app import run

if __name__ == "__main__":
    run()
