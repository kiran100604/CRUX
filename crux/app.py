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

from .config import Config
from .hotkey import chord_label, pynput_hotkey
from .quickcapture import _save, flash_toast


def _icon_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=(20, 48, 48, 255))       # teal disc
    d.ellipse((25, 25, 39, 39), fill=(253, 248, 236, 255))  # cream core
    return img


def _is_wayland() -> bool:
    import os
    return (os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
            or bool(os.environ.get("WAYLAND_DISPLAY")))


def _warn_wayland(chord: str) -> None:
    """On Wayland the compositor blocks apps from grabbing global keys / injecting
    keystrokes, so this in-app hotkey usually won't fire. Point users to the
    reliable path: bind `crux capture` to a shortcut in the OS settings."""
    if sys.platform != "linux" or not _is_wayland():
        return
    print(
        "\n[crux] ⚠ Wayland detected. The in-app hotkey only sees keys when an\n"
        "       XWayland window is focused — so it WON'T fire from native Wayland\n"
        "       apps (e.g. Chrome). The reliable fix is a compositor-level binding:\n\n"
        "           crux bind          ← one command: binds your chord to capture\n\n"
        "       Then: select text anywhere → Ctrl+C → press " + chord + ".\n"
        "       (Quit this with Ctrl+C; you don't need `crux app` once bound —\n"
        "        just keep `crux serve` running for the dashboard.)\n",
        flush=True)


def _copy_selection() -> None:
    """Send Ctrl/Cmd+C to copy the current selection.

    Critical: when the hotkey fires, the user is usually still holding the chord
    modifiers (e.g. Ctrl+Shift). A naive Ctrl+C would be read as Ctrl+Shift+C —
    NOT copy — so nothing gets copied. We first release any held modifiers, then
    send a clean Ctrl/Cmd+C.
    """
    try:
        from pynput.keyboard import Controller, Key
        kb = Controller()
        # neutralize chord modifiers the user is still pressing
        for k in (Key.shift, Key.shift_r, Key.ctrl, Key.ctrl_r,
                  Key.alt, Key.alt_r, Key.alt_gr, Key.cmd, Key.cmd_r):
            try:
                kb.release(k)
            except Exception:
                pass
        time.sleep(0.06)
        mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
        with kb.pressed(mod):
            kb.press("c"); kb.release("c")
        time.sleep(0.12)
    except Exception:
        pass  # fall back to whatever is already on the clipboard


def _notify(msg: str) -> None:
    """Desktop toast on Linux (native, avoids tkinter); print elsewhere/fallback."""
    import shutil
    import subprocess
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "-t", "2500", "CRUX", msg], timeout=3)
            return
        except Exception:
            pass
    print("[crux]", msg, flush=True)


def _run_linux(cfg, hotkey, chord, base, url, open_dashboard) -> None:
    """Linux path: NO tkinter. pynput's X listener conflicts with a tkinter mainloop
    in the same process (the hotkey silently stops firing), so we drive capture with
    pynput only and confirm via notify-send. Reliable flow: copy (Ctrl+C) → hotkey."""
    from pynput import keyboard

    from .cli import read_clipboard
    last = {"clip": None}

    def _capture():
        cur = (read_clipboard() or "").strip()
        if not cur:
            _notify("Clipboard empty — copy text (Ctrl+C) first, then press the hotkey")
            return
        if cur == last["clip"]:
            _notify("Already captured — copy new text first")
            return
        last["clip"] = cur
        print(f"[crux] capturing {len(cur)} chars", flush=True)
        msg = _save(cfg, cur)
        print(f"[crux] saved → {msg}", flush=True)
        _notify(msg)

    def on_hotkey():
        print(f"[crux] hotkey fired ({chord})", flush=True)
        threading.Thread(target=_capture, daemon=True).start()

    try:
        hk = keyboard.GlobalHotKeys({hotkey: on_hotkey})
        hk.start()
    except Exception as e:
        raise SystemExit(f"Could not register the hotkey '{chord}' ({hotkey}): {e}")
    print(f"[crux] listening for hotkey: {hotkey}", flush=True)
    _warn_wayland(chord)
    if open_dashboard:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"CRUX is running. Copy text (Ctrl+C), then press {chord}. Dashboard: {base}")
    print("(Ctrl+C here to quit)")
    try:
        hk.join()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            hk.stop()
        except Exception:
            pass


def run(open_dashboard: bool = True) -> None:
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

    # dashboard server in the background (shared by all platforms)
    def _serve_bg():
        try:
            from .server import run as serve_run
            serve_run(cfg)
        except Exception as e:  # pragma: no cover
            print("[crux] dashboard server not started:", e, file=sys.stderr)
    threading.Thread(target=_serve_bg, daemon=True).start()

    if sys.platform == "linux":
        _run_linux(cfg, hotkey, chord, base, url, open_dashboard)
        return

    try:
        import tkinter as tk
    except Exception as e:  # pragma: no cover
        raise SystemExit(
            f"The app needs tkinter ({e}). It ships with Python on Windows/macOS.")

    # hidden root owns the GUI thread; overlays are Toplevels off it
    root = tk.Tk()
    root.withdraw()

    def _read_clip_native() -> str:
        from .cli import read_clipboard       # wl-paste / xclip / xsel / pbpaste
        return read_clipboard() or ""

    def _read_clip_tk() -> str:
        return root.clipboard_get()

    def _read_clip(label: str) -> str:
        """Read the clipboard, logging exactly what happens. On Linux prefer the
        native tool (wl-paste reads the real Wayland clipboard; tkinter only sees
        the XWayland one, which can be stale). Elsewhere prefer tkinter (fast)."""
        order = ([("native", _read_clip_native), ("tk", _read_clip_tk)]
                 if sys.platform == "linux"
                 else [("tk", _read_clip_tk), ("native", _read_clip_native)])
        for name, fn in order:
            try:
                v = fn() or ""
                if v:
                    print(f"[crux]   clip[{label}] via {name}: {len(v)} chars {v[:40]!r}", flush=True)
                    return v
            except Exception as e:
                print(f"[crux]   clip[{label}] {name} failed: {e!r}", flush=True)
        print(f"[crux]   clip[{label}] empty", flush=True)
        return ""

    def _save_and_flash(text: str) -> None:
        # worker thread: DB write can be slow, keep it off the GUI/hook threads
        try:
            msg = _save(cfg, text)
            print(f"[crux] saved → {msg}", flush=True)
            root.after(0, lambda: flash_toast(root, msg, ok=True))
        except Exception:
            import traceback; traceback.print_exc()

    last = {"clip": None}

    def _do_capture() -> None:
        # Flow: try to auto-copy the selection (works where SendInput is allowed),
        # then capture whatever is on the clipboard. On locked-down machines the
        # synthetic copy is blocked, so the reliable flow is: user copies with
        # their own Ctrl+C, then presses the hotkey to file it.
        try:
            print("[crux] --- capture start ---", flush=True)
            _copy_selection()                 # best-effort (no-op if injection blocked)
            cur = _read_clip("clipboard").strip()
            if not cur:
                flash_toast(root, "Clipboard empty — copy text first, then press the hotkey", ok=False)
                return
            if cur == last["clip"]:
                print("[crux] clipboard unchanged since last capture", flush=True)
                flash_toast(root, "Already captured — copy new text first", ok=False)
                return
            last["clip"] = cur
            print(f"[crux] capturing {len(cur)} chars", flush=True)
            threading.Thread(target=lambda: _save_and_flash(cur), daemon=True).start()
        except Exception:
            import traceback; traceback.print_exc()

    def on_hotkey() -> None:
        # runs in the pynput hook thread — must return INSTANTLY or the whole
        # keyboard lags. Just hand the work to the GUI thread and get out.
        print(f"[crux] hotkey fired ({chord})", flush=True)
        root.after(0, _do_capture)

    try:
        hk = keyboard.GlobalHotKeys({hotkey: on_hotkey})
        hk.start()
    except Exception as e:
        raise SystemExit(
            f"Could not register the hotkey '{chord}' ({hotkey}): {e}\n"
            "Try a different chord with `crux setup` (e.g. Ctrl+Shift+K), or use the\n"
            "tray menu's 'Capture now' / the dashboard capture box instead.")
    print(f"[crux] listening for hotkey: {hotkey}", flush=True)
    _warn_wayland(chord)

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
    print(f"CRUX is running. To capture: copy text (Ctrl+C), then press {chord}.")
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
