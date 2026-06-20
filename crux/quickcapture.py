"""Quick-capture popup — a small, always-on-top window for instant, *visible*
capture. Press the hotkey (or run `crux popup`) and a box appears, pre-filled
with your current selection/clipboard; Enter saves to CRUX, Esc cancels.

Uses tkinter (Python standard library) so there are no extra GUI dependencies.
The whole point is feedback: you see exactly what will be saved, like the
Win+Shift+S snip overlay — no "did anything happen?" uncertainty.
"""

from __future__ import annotations

_BG = "#fdf8ec"      # canvas
_FG = "#2a251e"      # body text
_MUT = "#83785f"     # muted
_LINE = "#d6ccb2"    # hairline
_INK = "#080808"


def build_popup(root, initial: str, on_submit, on_cancel):
    """Create the capture window. `root` is a hidden Tk root (app mode) or None
    (standalone — we create our own Tk). Returns the window so the caller can
    run/await it."""
    import tkinter as tk

    standalone = root is None
    win = tk.Tk() if standalone else tk.Toplevel(root)
    win.title("Capture to CRUX")
    win.configure(bg=_BG)
    try:
        win.attributes("-topmost", True)
    except Exception:
        pass

    W, H = 580, 230
    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 3}")
    try:
        win.minsize(W, H)
    except Exception:
        pass

    tk.Label(win, text="CAPTURE TO CRUX", bg=_BG, fg=_MUT,
             font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=20, pady=(18, 8))

    txt = tk.Text(win, height=5, wrap="word", font=("Segoe UI", 12),
                  bg="#ffffff", fg=_FG, relief="flat", padx=12, pady=10,
                  insertbackground=_INK, highlightthickness=1,
                  highlightbackground=_LINE, highlightcolor=_INK)
    txt.pack(fill="both", expand=True, padx=20)
    if initial:
        txt.insert("1.0", initial)
    txt.focus_force()

    tk.Label(win, text="Enter = save     Shift+Enter = new line     Esc = cancel",
             bg=_BG, fg=_MUT, font=("Segoe UI", 9)).pack(anchor="w", padx=20, pady=(10, 16))

    def _destroy():
        try:
            win.destroy()
        except Exception:
            pass

    def submit(_e=None):
        val = txt.get("1.0", "end").strip()
        _destroy()
        on_submit(val)
        return "break"

    def cancel(_e=None):
        _destroy()
        on_cancel()
        return "break"

    # Plain Enter saves; Shift+Enter inserts a newline (default Text behavior).
    txt.bind("<Return>", submit)
    txt.bind("<Shift-Return>", lambda _e: None)
    win.bind("<Escape>", cancel)
    win.protocol("WM_DELETE_WINDOW", cancel)
    try:
        win.lift()
        win.focus_force()
    except Exception:
        pass
    return win


def _save(cfg, text: str) -> str:
    """Persist captured text into working memory as a raw step on the current
    thread (kept as narrative, not atomized into facts)."""
    from .store import Store
    store = Store(cfg)
    try:
        res = store.add_step(text, source="popup")
        where = "current thread" if res.get("thread_id") else "working memory"
        return f"Added to {where}: {text.strip()[:44]}"
    finally:
        store.close()


def _safe_destroy(win) -> None:
    try:
        win.destroy()
    except Exception:
        pass


def flash_toast(root, text: str, ok: bool = True):
    """A brief, borderless 'snap' overlay that fades in/out — the visual
    confirmation that a capture landed (no buttons, auto-dismisses)."""
    import tkinter as tk
    win = tk.Toplevel(root)
    win.overrideredirect(True)          # no title bar / borders
    try:
        win.attributes("-topmost", True)
    except Exception:
        pass
    bg = "#143030" if ok else "#bf3422"
    mark = "✓" if ok else "✕"
    frame = tk.Frame(win, bg=bg, highlightthickness=0)
    frame.pack(fill="both", expand=True)
    tk.Label(frame, text=f"{mark}  {text}", bg=bg, fg="#ffffff",
             font=("Segoe UI", 13, "bold"), padx=26, pady=16).pack()
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"+{(sw - w) // 2}+{sh - h - 90}")   # bottom-center, like a snip toast

    def fade(alpha, step, done):
        nxt = alpha + step
        try:
            win.attributes("-alpha", max(0.0, min(0.96, nxt)))
        except Exception:
            pass
        if (step > 0 and nxt < 0.96) or (step < 0 and nxt > 0.0):
            win.after(16, lambda: fade(nxt, step, done))
        else:
            done()

    try:
        win.attributes("-alpha", 0.0)
    except Exception:
        pass
    fade(0.0, 0.14, lambda: win.after(950, lambda: fade(0.96, -0.10, lambda: _safe_destroy(win))))
    return win


def flash_standalone(message: str, ok: bool = True, ms: int = 1500) -> bool:
    """Show the dark flash toast from a one-shot process (e.g. the bound
    `crux capture`). Creates its own short-lived root, plays the fade, exits.
    Returns False if there's no tkinter/display so the caller can fall back."""
    try:
        import tkinter as tk
    except Exception:
        return False
    try:
        root = tk.Tk()
        root.withdraw()
    except Exception:
        return False  # no display
    flash_toast(root, message, ok=ok)
    root.after(ms, root.quit)
    try:
        root.mainloop()
    except Exception:
        pass
    try:
        root.destroy()
    except Exception:
        pass
    return True


def hint_toast(root, text: str):
    """A small top-center hint shown while we wait for the user to select text.
    Returns the window so the caller can dismiss it once capture happens."""
    import tkinter as tk
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    try:
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.94)
    except Exception:
        pass
    frame = tk.Frame(win, bg="#fdf8ec", highlightthickness=1, highlightbackground="#d6ccb2")
    frame.pack(fill="both", expand=True)
    tk.Label(frame, text=f"✂  {text}", bg="#fdf8ec", fg="#2a251e",
             font=("Segoe UI", 11), padx=20, pady=11).pack()
    win.update_idletasks()
    w = win.winfo_width()
    sw = win.winfo_screenwidth()
    win.geometry(f"+{(sw - w) // 2}+40")
    return win


def run_standalone(cfg, initial: str | None = None) -> str:
    """`crux popup`: open the capture box once, save on Enter, return a status."""
    try:
        import tkinter  # noqa: F401
    except Exception as e:  # pragma: no cover
        raise SystemExit(
            f"The capture popup needs tkinter ({e}). On most systems it ships with "
            "Python; on Linux: sudo apt install python3-tk.")
    from .cli import read_clipboard

    if initial is None:
        initial = (read_clipboard() or "").strip()
    holder: dict[str, str] = {}
    win = build_popup(None, initial,
                      lambda v: holder.update(value=v),
                      lambda: None)
    win.mainloop()
    val = holder.get("value", "").strip()
    if not val:
        return "Cancelled — nothing captured."
    return _save(cfg, val)
