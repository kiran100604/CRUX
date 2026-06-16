# PyInstaller spec — builds CRUX into a standalone desktop app.
#   cd packaging && pyinstaller crux.spec
# Produces dist/CRUX (folder app) and, on macOS, dist/CRUX.app (menubar agent).
import sys
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# Dynamic-import-heavy packages need their submodules collected explicitly.
hidden = []
for pkg in ("crux", "uvicorn", "fastapi", "pystray", "pynput", "anthropic", "openai"):
    try:
        hidden += collect_submodules(pkg)
    except Exception:
        pass  # optional providers may be absent at build time

# Bundle the dashboard SPA (crux/static/index.html) and any package data.
datas = collect_data_files("crux")

a = Analysis(
    ["entry.py"],
    pathex=[".."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="CRUX",
    console=False,           # no terminal window — it's a tray app
    disable_windowed_traceback=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="CRUX")

# macOS: wrap into a .app as a background "agent" (LSUIElement = menubar only, no Dock icon)
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="CRUX.app",
        icon=None,  # drop an .icns path here when you have a logo
        bundle_identifier="ai.crux.desktop",
        info_plist={
            "LSUIElement": True,
            "CFBundleName": "CRUX",
            "CFBundleDisplayName": "CRUX",
            "NSHighResolutionCapable": True,
        },
    )
