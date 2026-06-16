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
    """Persist captured text; long/multiline → ingest as a doc, else a note."""
    from .store import Store
    store = Store(cfg)
    try:
        if len(text) > 400 or "\n" in text:
            res = store.ingest(text, source_type="paste", source_ref="popup")
            return f"Captured → {len(res['facts'])} fact(s)"
        item = store.capture(text, source_type="popup")
        return f"Captured: {item.title[:48]}"
    finally:
        store.close()


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
