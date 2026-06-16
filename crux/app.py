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
from .quickcapture import _save, flash_toast, hint_toast


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

    # hidden root owns the GUI thread; overlays are Toplevels off it
    root = tk.Tk()
    root.withdraw()

    arm = {"listener": None, "hint": None}

    def _capture(text: str) -> None:
        """Save (in this worker thread) then flash the result on the GUI thread."""
        text = (text or "").strip()
        if not text:
            root.after(0, lambda: flash_toast(root, "Nothing selected", ok=False))
            return
        msg = _save(cfg, text)
        root.after(0, lambda: flash_toast(root, msg, ok=True))

    def _disarm() -> None:
        if arm["listener"] is not None:
            try:
                arm["listener"].stop()
            except Exception:
                pass
            arm["listener"] = None
        if arm["hint"] is not None:
            try:
                arm["hint"].destroy()
            except Exception:
                pass
            arm["hint"] = None

    def _arm_selection(baseline: str) -> None:
        """Press-then-select: wait for the next mouse selection, then capture it."""
        from pynput import mouse

        def on_click(x, y, button, pressed):
            if pressed:
                return
            # mouse released → try to grab whatever is now selected
            _copy_selection()
            cur = (read_clipboard() or "")
            if cur.strip() and cur != baseline:
                root.after(0, _disarm)
                _capture(cur)
                return False  # stop listener
            return None

        listener = mouse.Listener(on_click=on_click)
        arm["listener"] = listener
        listener.start()
        # show the hint and auto-cancel after 12s of no selection
        arm["hint"] = hint_toast(root, "Select text to capture…")
        root.after(12000, _disarm)

    def on_hotkey() -> None:
        # runs in the pynput thread.
        print(f"[crux] hotkey fired ({chord})", flush=True)
        _disarm()  # a second press cancels an armed selection
        prev = (read_clipboard() or "")
        _copy_selection()
        cur = (read_clipboard() or "")
        if cur.strip() and cur != prev:
            print(f"[crux] captured selection ({len(cur)} chars)", flush=True)
            _capture(cur)               # text was already selected → instant snap
        else:
            print("[crux] no selection yet — waiting for you to select text", flush=True)
            root.after(0, lambda: _arm_selection(prev))  # nothing selected → wait for it

    try:
        hk = keyboard.GlobalHotKeys({hotkey: on_hotkey})
        hk.start()
    except Exception as e:
        raise SystemExit(
            f"Could not register the hotkey '{chord}' ({hotkey}): {e}\n"
            "Try a different chord with `crux setup` (e.g. Ctrl+Shift+K), or use the\n"
            "tray menu's 'Capture now' / the dashboard capture box instead.")
    print(f"[crux] listening for hotkey: {hotkey}", flush=True)

    # best-effort tray icon (works on Windows/Linux in a thread; mac needs main
    # thread, so we just skip it there and rely on the hotkey + dashboard).
    icon = None
    if sys.platform != "darwin":
        try:
            import pystray

            def open_dash(*_): webbrowser.open(base)
            def capture_now(*_): on_hotkey()
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
