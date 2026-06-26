"""Install a taskbar/menu launcher for the quick-capture popup.

The whole point: ONE click inside CRUX ("Add to taskbar") drops an app icon you
can pin. Clicking that icon runs `crux popup` — which reads the clipboard and
shows the tag picker — so capture is: copy text → click icon → pick a tag. No
terminal, nothing to install by hand.

Each launcher just runs the popup as a one-shot process (no daemon to keep
alive), and the popup writes straight to the local DB, so it works whether or not
the dashboard server happens to be running.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _popup_command() -> list[str]:
    """The command the launcher runs. Prefer the installed `crux` entry point so a
    packaged build has no console window; fall back to the module form."""
    import shutil
    exe = "pythonw.exe" if sys.platform == "win32" else None
    crux = shutil.which("crux")
    if crux:
        return [crux, "popup"]
    py = sys.executable
    if exe and py.lower().endswith("python.exe"):
        cand = Path(py).with_name(exe)
        if cand.exists():
            py = str(cand)
    return [py, "-m", "crux.cli", "popup"]


def _write_icon(home: Path) -> str | None:
    """Render CRUX's mark to a PNG so the launcher has a real icon. Best-effort —
    returns the path, or None if Pillow isn't available (launcher still works,
    just with a generic icon)."""
    out = home / "crux.png"
    if out.exists():
        return str(out)
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    img = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((10, 10, 118, 118), fill=(20, 48, 48, 255))      # teal disc
    d.ellipse((50, 50, 78, 78), fill=(253, 248, 236, 255))     # cream core
    try:
        home.mkdir(parents=True, exist_ok=True)
        img.save(out)
        return str(out)
    except Exception:
        return None


def _install_linux(home: Path) -> dict:
    apps = Path.home() / ".local" / "share" / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    icon = _write_icon(home) or "accessories-text-editor"
    cmd = " ".join(_quote(a) for a in _popup_command())
    dest = apps / "crux-capture.desktop"
    dest.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Capture to CRUX\n"
        "Comment=Capture the copied text into CRUX and tag it\n"
        f"Exec={cmd}\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
        "Keywords=crux;capture;context;\n",
        encoding="utf-8")
    try:
        os.chmod(dest, 0o755)
    except Exception:
        pass
    # refresh the menu so the entry shows up immediately (best-effort)
    import shutil
    import subprocess
    if shutil.which("update-desktop-database"):
        try:
            subprocess.run(["update-desktop-database", str(apps)], timeout=5)
        except Exception:
            pass
    return {"ok": True, "path": str(dest),
            "message": ("Added “Capture to CRUX” to your apps. Open your app menu, "
                        "find it, and right-click → Pin to taskbar/favourites. Then: "
                        "copy text anywhere → click it → pick a tag.")}


def _install_windows(home: Path) -> dict:
    import subprocess
    cmd = _popup_command()
    target, args = cmd[0], " ".join(_quote(a) for a in cmd[1:])
    programs = Path(os.environ.get("APPDATA", Path.home())) / \
        "Microsoft" / "Windows" / "Start Menu" / "Programs"
    programs.mkdir(parents=True, exist_ok=True)
    lnk = programs / "Capture to CRUX.lnk"
    icon = _write_icon(home)
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
        "$s.TargetPath='{target}';$s.Arguments='{args}';"
        "$s.Description='Capture to CRUX';{icon}$s.Save()"
    ).format(lnk=str(lnk), target=target, args=args.replace("'", "''"),
             icon=(f"$s.IconLocation='{icon}';" if icon and icon.endswith(('.ico',)) else ""))
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=15, check=True)
    except Exception as e:
        return {"ok": False, "path": str(lnk),
                "message": f"Couldn't create the shortcut automatically ({e})."}
    return {"ok": True, "path": str(lnk),
            "message": ("Added “Capture to CRUX” to your Start Menu. Right-click it → "
                        "Pin to taskbar. Then: copy text → click it → pick a tag.")}


def _install_macos(home: Path) -> dict:
    cmd = " ".join(_quote(a) for a in _popup_command())
    dest = Path.home() / "Applications"
    dest.mkdir(parents=True, exist_ok=True)
    sh = dest / "Capture to CRUX.command"
    sh.write_text(f"#!/bin/sh\n{cmd}\n", encoding="utf-8")
    try:
        os.chmod(sh, 0o755)
    except Exception:
        pass
    return {"ok": True, "path": str(sh),
            "message": ("Created “Capture to CRUX” in ~/Applications. Drag it to your "
                        "Dock to pin it. Then: copy text → click it → pick a tag.")}


def _quote(arg: str) -> str:
    """Quote a path/arg for an Exec/shortcut line if it contains spaces."""
    return f'"{arg}"' if (" " in arg and not arg.startswith('"')) else arg


def install_launcher(home: Path | str | None = None) -> dict:
    """Install the OS launcher for the capture popup. Returns
    {ok, path, message}. home is CRUX's data dir (for the icon file)."""
    home = Path(home) if home else Path.home() / ".crux"
    if sys.platform == "win32":
        return _install_windows(home)
    if sys.platform == "darwin":
        return _install_macos(home)
    if sys.platform.startswith("linux"):
        return _install_linux(home)
    return {"ok": False, "path": "",
            "message": f"Unsupported platform: {sys.platform}"}
