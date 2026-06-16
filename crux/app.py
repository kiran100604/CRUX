"""`crux app` — a tray / menubar app that makes CRUX feel like an appliance.

It owns the global capture hotkey itself (no manual Hammerspoon/GNOME binding),
runs the dashboard in the background, and gives a tray menu. "Select text
anywhere → press the hotkey → it's in CRUX."

Optional native deps:  pip install 'crux[app]'  (pystray, pynput, pillow)
macOS note: needs Accessibility permission (System Settings → Privacy →
Accessibility) for the global hotkey + auto-copy to work.
"""

from __future__ import annotations

import sys
import threading
import time
import webbrowser

from .cli import read_clipboard
from .config import Config
from .store import Store

# pynput hotkey syntax; Cmd on macOS, Ctrl elsewhere
HOTKEY = "<cmd>+<shift>+<space>" if sys.platform == "darwin" else "<ctrl>+<shift>+<space>"
CHORD_LABEL = "Cmd+Shift+Space" if sys.platform == "darwin" else "Ctrl+Shift+Space"


def _icon_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=(26, 58, 58, 255))      # teal disc
    d.ellipse((25, 25, 39, 39), fill=(255, 250, 240, 255))  # cream core
    return img


def _do_capture(notify) -> None:
    """Copy the current selection, then capture the clipboard."""
    try:
        from pynput.keyboard import Controller, Key
        kb = Controller()
        mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
        with kb.pressed(mod):
            kb.press("c"); kb.release("c")
        time.sleep(0.15)
    except Exception:
        pass  # fall back to whatever is already on the clipboard

    text = read_clipboard()
    if not text or not text.strip():
        notify("Nothing to capture")
        return
    store = Store(Config.load())
    try:
        if len(text) > 400 or "\n" in text:
            res = store.ingest(text, source_type="paste", source_ref="hotkey")
            notify(f"Captured → {len(res['facts'])} fact(s)")
        else:
            it = store.capture(text, source_type="hotkey")
            notify(f"Captured: {it.title[:40]}")
    finally:
        store.close()


def run(open_dashboard: bool = True) -> None:
    try:
        import pystray
        from pynput import keyboard
    except Exception as e:  # missing native deps
        raise SystemExit(
            f"The tray app needs extra packages ({e}).\n"
            f"Install with:  pip install 'crux[app]'")

    cfg = Config.load()
    cfg.ensure_home()
    base = f"http://{cfg.host}:{cfg.port}"
    # First launch → open the in-browser setup wizard; afterwards, the dashboard.
    url = base if cfg.is_configured() else f"{base}/setup"

    # dashboard server in the background so "Open dashboard" always works
    def _serve():
        try:
            from .server import run as serve_run
            serve_run(cfg)
        except Exception as e:  # pragma: no cover
            print("[crux] dashboard server not started:", e, file=sys.stderr)
    threading.Thread(target=_serve, daemon=True).start()

    icon = None

    def notify(msg: str) -> None:
        try:
            icon.notify(msg, "CRUX")
        except Exception:
            print("[crux]", msg)

    def on_hotkey() -> None:
        threading.Thread(target=_do_capture, args=(notify,), daemon=True).start()

    hk = keyboard.GlobalHotKeys({HOTKEY: on_hotkey})
    hk.start()

    def open_dash(*_): webbrowser.open(base)
    def open_first(*_): webbrowser.open(url)
    def capture_now(*_): on_hotkey()
    def quit_app(*_):
        hk.stop(); icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open CRUX dashboard", open_dash, default=True),
        pystray.MenuItem("Capture clipboard now", capture_now),
        pystray.MenuItem("Quit", quit_app),
    )
    icon = pystray.Icon("crux", _icon_image(), "CRUX", menu)
    if open_dashboard:
        threading.Timer(1.2, open_first).start()
    print(f"CRUX is running in your tray. Hotkey: {CHORD_LABEL}. Dashboard: {url}")
    icon.run()  # blocks on the main thread (required on macOS)
