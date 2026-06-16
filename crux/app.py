"""`crux app` — a background appliance that makes capture feel like Win+Shift+S.

Press your hotkey anywhere → a small box pops up pre-filled with the text you
had selected → Enter saves it to CRUX. You always *see* what's captured.

Architecture (reliability first):
  • a hidden tkinter root owns the main thread and shows the popup (GUI must be
    on the main thread);
  • a pynput global-hotkey listener runs in a background thread and marshals to
    the GUI via root.after();
  • the dashboard server and the (best-effort) tray icon run in daemon threads.

Optional native deps:  pip install 'crux[app]'  (pynput; pystray/pillow for the
tray icon). tkinter ships with Python (Linux: sudo apt install python3-tk).
macOS note: the global hotkey + auto-copy need Accessibility permission
(System Settings → Privacy → Accessibility).
"""

from __future__ import annotations

import sys
import threading
import time
import webbrowser

from .cli import read_clipboard
from .config import Config
from .hotkey import chord_label, pynput_hotkey
from .quickcapture import _save, build_popup


def _icon_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=(20, 48, 48, 255))       # teal disc
    d.ellipse((25, 25, 39, 39), fill=(253, 248, 236, 255))  # cream core
    return img


def _copy_selection() -> None:
    """Send Ctrl/Cmd+C so the popup can pre-fill with the highlighted text."""
    try:
        from pynput.keyboard import Controller, Key
        kb = Controller()
        mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
        with kb.pressed(mod):
            kb.press("c"); kb.release("c")
        time.sleep(0.15)
    except Exception:
        pass  # fall back to whatever is already on the clipboard


def run(open_dashboard: bool = True) -> None:
    try:
        import tkinter as tk
    except Exception as e:  # pragma: no cover
        raise SystemExit(
            f"The app needs tkinter ({e}). It ships with Python on Windows/macOS; "
            "on Linux run: sudo apt install python3-tk")
    try:
        from pynput import keyboard
    except Exception as e:  # missing native deps
        raise SystemExit(
            f"The app needs pynput ({e}).  Install with:  pip install 'crux[app]'")

    cfg = Config.load()
    cfg.ensure_home()
    base = f"http://{cfg.host}:{cfg.port}"
    url = base if cfg.is_configured() else f"{base}/setup"
    hotkey = pynput_hotkey(cfg.hotkey_mods, cfg.hotkey_key)
    chord = chord_label(cfg.hotkey_mods, cfg.hotkey_key)

    # dashboard server in the background so "Open dashboard" always works
    def _serve():
        try:
            from .server import run as serve_run
            serve_run(cfg)
        except Exception as e:  # pragma: no cover
            print("[crux] dashboard server not started:", e, file=sys.stderr)
    threading.Thread(target=_serve, daemon=True).start()

    # hidden root owns the GUI thread; popups are Toplevels off it
    root = tk.Tk()
    root.withdraw()

    def _toast(msg: str) -> None:
        print("[crux]", msg)

    def _show_popup(initial: str) -> None:
        def on_submit(val: str):
            val = (val or "").strip()
            _toast(_save(cfg, val) if val else "Cancelled — nothing captured.")
        build_popup(root, initial, on_submit, lambda: None)

    def on_hotkey() -> None:
        # runs in the pynput thread: copy selection, then hand off to the GUI thread
        _copy_selection()
        text = (read_clipboard() or "").strip()
        root.after(0, lambda: _show_popup(text))

    hk = keyboard.GlobalHotKeys({hotkey: on_hotkey})
    hk.start()

    # best-effort tray icon (works on Windows/Linux in a thread; mac needs main
    # thread, so we just skip it there and rely on the hotkey + dashboard).
    icon = None
    if sys.platform != "darwin":
        try:
            import pystray

            def open_dash(*_): webbrowser.open(base)
            def capture_now(*_): root.after(0, lambda: _show_popup((read_clipboard() or "").strip()))
            def quit_app(*_):
                hk.stop()
                if icon:
                    icon.stop()
                root.after(0, root.destroy)

            menu = pystray.Menu(
                pystray.MenuItem("Open CRUX dashboard", open_dash, default=True),
                pystray.MenuItem("Capture now", capture_now),
                pystray.MenuItem("Quit", quit_app),
            )
            icon = pystray.Icon("crux", _icon_image(), "CRUX", menu)
            threading.Thread(target=icon.run, daemon=True).start()
        except Exception as e:  # pragma: no cover
            print("[crux] tray icon unavailable:", e, file=sys.stderr)

    if open_dashboard:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"CRUX is running. Hotkey: {chord}  →  select text anywhere and press it.")
    print(f"Dashboard: {base}   (Ctrl+C here to quit)")
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        hk.stop()
        if icon:
            try:
                icon.stop()
            except Exception:
                pass
