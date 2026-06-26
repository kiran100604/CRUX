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
    """The command the launcher runs. On Windows use pythonw.exe so NO console
    window appears behind the popup (the `crux.exe` console entry point flashes a
    black terminal — the 'black screen' bug). Elsewhere the `crux` entry point is
    fine."""
    import shutil
    if sys.platform == "win32":
        py = sys.executable
        cand = Path(py).with_name("pythonw.exe")   # windowless interpreter
        if cand.exists():
            py = str(cand)
        return [py, "-m", "crux.cli", "popup"]
    crux = shutil.which("crux")
    if crux:
        return [crux, "popup"]
    return [sys.executable, "-m", "crux.cli", "popup"]


def _render_icon_png(size: int = 128) -> bytes:
    """CRUX's mark as a PNG, drawn in pure Python (no Pillow): a teal disc with a
    cream core on a transparent ground. Antialiased edges so it looks crisp at
    taskbar size."""
    import struct
    import zlib

    cx = cy = size / 2.0
    r_out = size * 0.46
    r_in = size * 0.13
    teal = (20, 48, 48)
    cream = (253, 248, 236)

    def _cov(d: float, edge: float) -> float:  # 1px-soft edge coverage
        return max(0.0, min(1.0, (edge - d) + 0.5))

    raw = bytearray()
    for y in range(size):
        raw.append(0)  # PNG filter type 0 for the row
        for x in range(size):
            d = ((x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2) ** 0.5
            a_out = _cov(d, r_out)
            if a_out <= 0:
                raw.extend((0, 0, 0, 0)); continue
            a_in = _cov(d, r_in)
            r = int(cream[0] * a_in + teal[0] * (1 - a_in))
            g = int(cream[1] * a_in + teal[1] * (1 - a_in))
            b = int(cream[2] * a_in + teal[2] * (1 - a_in))
            raw.extend((r, g, b, int(255 * a_out)))

    def _chunk(typ: bytes, data: bytes) -> bytes:
        body = typ + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


def _png_to_ico(png: bytes, size: int = 128) -> bytes:
    """Wrap a PNG as a single-image .ico (Windows Vista+ reads PNG-in-ICO)."""
    import struct
    dim = size if size < 256 else 0
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), 22)
    return header + entry + png


def _write_icons(home: Path) -> tuple[str | None, str | None]:
    """Write the launcher icon as PNG (Linux) and ICO (Windows) into CRUX's data
    dir. Pure-Python, so it always succeeds; returns (png_path, ico_path)."""
    try:
        home.mkdir(parents=True, exist_ok=True)
        png = _render_icon_png(128)
        png_path = home / "crux.png"
        png_path.write_bytes(png)
        ico_path = home / "crux.ico"
        ico_path.write_bytes(_png_to_ico(png, 128))
        return str(png_path), str(ico_path)
    except Exception:
        return None, None


def _install_linux(home: Path) -> dict:
    apps = Path.home() / ".local" / "share" / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    png, _ = _write_icons(home)
    icon = png or "accessories-text-editor"
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
    _, ico = _write_icons(home)
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
        "$s.TargetPath='{target}';$s.Arguments='{args}';"
        "$s.Description='Capture to CRUX';{icon}$s.Save()"
    ).format(lnk=str(lnk), target=target, args=args.replace("'", "''"),
             icon=(f"$s.IconLocation='{ico}';" if ico else ""))
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
