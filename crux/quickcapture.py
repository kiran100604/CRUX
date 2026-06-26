"""Quick-capture popup — a small, always-on-top window for instant, *visible*
capture. Press the hotkey (or run `crux popup`) and a box appears, pre-filled
with your current selection/clipboard; Enter saves to CRUX, Esc cancels.

Uses tkinter (Python standard library) so there are no extra GUI dependencies.
The whole point is feedback: you see exactly what will be saved, like the
Win+Shift+S snip overlay — no "did anything happen?" uncertainty.
"""

from __future__ import annotations

# CRUX dark palette (mirrors tokens.css html.crux-dark) so the popup feels part
# of the product, not a stray OS dialog.
_BG = "#0f0f0f"      # canvas
_CARD = "#1f1f1f"    # paper (the text well)
_FG = "#e8e6e2"      # body text
_INKW = "#fafaf8"    # strong text / caret
_MUT = "#8b8876"     # muted
_LINE = "#404040"    # hairline
_TEAL = "#6fb3b0"    # accent
_TEAL_BG = "#15302e"

# The tags offered in the popup — what the capture IS, so CRUX treats it right
# (e.g. competitor info as a Reference, not our own decision). Picking one is the
# ONLY action: the text already rode in from the clipboard. Each pairs with a
# 1-key shortcut; "auto" (0 / Enter) lets CRUX classify. Colors are the kind
# taxonomy from the design tokens, so a Decision looks like a Decision.
_POPUP_TAGS = [
    ("1", "decision", "Decision", "#88b6ec", "#16263a"),
    ("2", "requirement", "Requirement", "#84c46f", "#1b2e19"),
    ("3", "constraint", "Constraint", "#ff6b5b", "#3a201c"),
    ("4", "reference", "Reference", "#6fb3b0", "#15302e"),
    ("5", "question", "Question", "#ff6b5b", "#3a201c"),
]


def build_popup(root, initial: str, on_submit, on_cancel):
    """Create the capture window. `root` is a hidden Tk root (app mode) or None
    (standalone — we create our own Tk). The captured text is pre-filled from the
    clipboard; the user's only action is clicking a TAG (or auto). on_submit is
    called as on_submit(text, kind) where kind is a card kind or None (auto).
    Returns the window so the caller can run/await it."""
    import tkinter as tk

    standalone = root is None
    win = tk.Tk() if standalone else tk.Toplevel(root)
    win.title("Capture to CRUX")
    win.configure(bg=_BG)
    try:
        win.attributes("-topmost", True)
    except Exception:
        pass

    W, H = 660, 330
    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 3}")
    try:
        win.minsize(W, H)
    except Exception:
        pass

    PAD = 24
    # Branded header: the CRUX mark (a teal dot) + wordmark, with an Esc hint.
    head = tk.Frame(win, bg=_BG)
    head.pack(fill="x", padx=PAD, pady=(18, 4))
    dot = tk.Canvas(head, width=14, height=14, bg=_BG, highlightthickness=0)
    dot.create_oval(1, 1, 13, 13, fill=_TEAL, outline="")
    dot.create_oval(5, 5, 9, 9, fill=_BG, outline="")
    dot.pack(side="left", pady=2)
    tk.Label(head, text="CAPTURE TO CRUX", bg=_BG, fg=_TEAL,
             font=("Segoe UI", 9, "bold")).pack(side="left", padx=(8, 0))
    tk.Label(head, text="Esc to cancel", bg=_BG, fg=_MUT,
             font=("Segoe UI", 8)).pack(side="right")

    # The clipboard text rode in automatically — show it (editable, in case you
    # want to trim), but it is NOT the thing you act on. The tag is.
    well = tk.Frame(win, bg=_LINE)                     # 1px frame = hairline border
    well.pack(fill="both", expand=True, padx=PAD, pady=(8, 4))
    txt = tk.Text(well, height=4, wrap="word", font=("Segoe UI", 12),
                  bg=_CARD, fg=_FG, relief="flat", padx=14, pady=12, bd=0,
                  insertbackground=_TEAL, highlightthickness=0,
                  selectbackground=_TEAL_BG, selectforeground=_INKW)
    txt.pack(fill="both", expand=True, padx=1, pady=1)
    if initial:
        txt.insert("1.0", initial)
        txt.focus_set()

    hint = ("What is this? Pick a tag — or press Enter to let CRUX sort it."
            if initial else
            "Nothing copied yet — copy text first and reopen, or type here, then tag.")
    tk.Label(win, text=hint, bg=_BG, fg=_MUT, anchor="w",
             font=("Segoe UI", 9)).pack(fill="x", padx=PAD, pady=(10, 8))

    def _destroy():
        try:
            win.destroy()
        except Exception:
            pass

    def submit(kind):
        val = txt.get("1.0", "end").strip()
        _destroy()
        on_submit(val, kind)
        return "break"

    def cancel(_e=None):
        _destroy()
        on_cancel()
        return "break"

    chips = tk.Frame(win, bg=_BG)
    chips.pack(fill="x", padx=PAD - 2, pady=(0, 18))

    def _chip(parent, label, key, kind, fg, bg):
        b = tk.Label(parent, text=f"{label} {key}", font=("Segoe UI", 10),
                     bg=_BG, fg=fg, padx=10, pady=6, cursor="hand2",
                     highlightthickness=1, highlightbackground=_LINE,
                     highlightcolor=_LINE)
        # hover = fill with the tag's own color, so it reads as that kind
        b.bind("<Enter>", lambda _e: b.configure(bg=bg, highlightbackground=fg))
        b.bind("<Leave>", lambda _e: b.configure(bg=_BG, highlightbackground=_LINE))
        b.bind("<Button-1>", lambda _e: submit(kind))
        return b

    for key, kind, label, fg, bg in _POPUP_TAGS:
        _chip(chips, label, f"·{key}", kind, fg, bg).pack(side="left", padx=4)
        win.bind(key, lambda _e, k=kind: submit(k))
    # Auto = the calm default: filled teal so it's the obvious primary action.
    auto = tk.Label(chips, text="Auto  ↵", font=("Segoe UI", 10, "bold"),
                    bg=_TEAL, fg=_BG, padx=14, pady=7, cursor="hand2",
                    highlightthickness=0)
    auto.bind("<Button-1>", lambda _e: submit(None))
    auto.bind("<Enter>", lambda _e: auto.configure(bg=_INKW))
    auto.bind("<Leave>", lambda _e: auto.configure(bg=_TEAL))
    auto.pack(side="right", padx=4)

    # Enter / 0 = auto-classify; Esc cancels. No tag to type, no save button.
    win.bind("<Return>", lambda _e: submit(None))
    win.bind("0", lambda _e: submit(None))
    win.bind("<Escape>", cancel)
    win.protocol("WM_DELETE_WINDOW", cancel)
    try:
        win.lift()
        win.focus_force()
    except Exception:
        pass
    return win


def _save(cfg, text: str, kind: str | None = None) -> str:
    """Persist captured text into working memory as a raw step on the current
    thread (kept as narrative, not atomized into facts). A `kind` is the user's
    tag — the card is born classified and treated by role (e.g. a Reference is
    context to be aware of, not folded in as our own decision)."""
    from .store import Store
    store = Store(cfg)
    try:
        res = store.add_step(text, source="popup", kind=kind)
        where = "current thread" if res.get("thread_id") else "working memory"
        tag = f" [{kind}]" if res.get("tagged") else ""
        return f"Added to {where}{tag}: {text.strip()[:40]}"
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


def _read_captured_text(root) -> str:
    """Get the text to capture from wherever the user put it. A taskbar/.desktop
    launch can't always read the clipboard the way a terminal can, so we try every
    source: the system clipboard tools, then tkinter's own clipboard, then (Linux)
    the PRIMARY selection — i.e. whatever is highlighted, no Ctrl+C needed."""
    from .cli import read_clipboard
    val = (read_clipboard() or "").strip()
    if val:
        return val
    for sel in ("CLIPBOARD", "PRIMARY"):
        try:
            v = (root.selection_get(selection=sel) or "").strip()
            if v:
                return v
        except Exception:
            pass
    return ""


def run_standalone(cfg, initial: str | None = None) -> str:
    """`crux popup`: open the capture box once. The popup ALWAYS appears (so a
    taskbar click always gives visible feedback); it's pre-filled with whatever
    you copied/selected. Pick a tag to save, Esc to cancel."""
    try:
        import tkinter as tk
    except Exception as e:  # pragma: no cover
        raise SystemExit(
            f"The capture popup needs tkinter ({e}). On most systems it ships with "
            "Python; on Linux: sudo apt install python3-tk.")

    # ONE hidden root owns the process — reused for the clipboard read and as the
    # parent of the popup (multiple tk.Tk() roots in one process are fragile).
    root = tk.Tk()
    root.withdraw()
    if initial is None:
        initial = _read_captured_text(root)
    holder: dict = {}

    def _finish():
        try:
            root.quit()
        except Exception:
            pass

    build_popup(root, initial,
                lambda v, kind: (holder.update(value=v, kind=kind), _finish()),
                _finish)
    try:
        root.mainloop()
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    val = (holder.get("value") or "").strip()
    if not val:
        return "Cancelled — nothing captured."
    return _save(cfg, val, holder.get("kind"))
