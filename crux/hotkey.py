"""Global hotkey setup for `crux capture`.

We can't register an OS-level hotkey from Python portably, so CRUX ships
copy-paste-ready snippets for each platform and writes them out with
`crux hotkey --install`. Each snippet copies the current selection, then runs
`crux capture` (which reads the clipboard) so "select anywhere → hotkey → it's in
CRUX" works.
"""

from __future__ import annotations

import sys
import re
from pathlib import Path

DEFAULT_MODS = ["ctrl", "shift"]
DEFAULT_KEY = "space"
DEFAULT_CHORD = "Cmd/Ctrl + Shift + Space"

# AutoHotkey v1 modifier symbols and Hammerspoon modifier names.
_AHK_MOD = {"ctrl": "^", "shift": "+", "alt": "!", "cmd": "#", "win": "#"}
_HS_MOD = {"ctrl": "ctrl", "shift": "shift", "alt": "alt", "cmd": "cmd", "win": "cmd"}


def chord_label(mods, key) -> str:
    return " + ".join([m.capitalize() for m in mods] + [key.capitalize()])


_ALLOWED_KEY = re.compile(r"^([a-z0-9]|space|f([1-9]|1[0-2]))$")
_RESERVED = {
    "ctrl+shift+i": "browser DevTools", "ctrl+shift+j": "browser DevTools",
    "ctrl+shift+c": "browser DevTools", "ctrl+shift+k": "browser console",
    "ctrl+w": "close tab", "ctrl+t": "new tab", "ctrl+n": "new window", "ctrl+q": "quit",
    "ctrl+c": "copy", "ctrl+v": "paste", "ctrl+x": "cut", "ctrl+z": "undo",
    "ctrl+a": "select all", "ctrl+s": "save", "alt+f4": "close window",
}


def valid_chord(mods, key) -> tuple[bool, str]:
    """Validate a custom chord (server-side safety net mirroring the wizard)."""
    if not key or not _ALLOWED_KEY.match(str(key)):
        return False, "key must be a letter, number, space, or F-key"
    if not any(m in ("ctrl", "alt", "cmd", "win") for m in mods):
        return False, "needs a Ctrl/Alt/Super modifier"
    if sys.platform != "darwin" and any(m in ("cmd", "win") for m in mods):
        return False, "Super/Win shortcuts are reserved by the desktop — use Ctrl/Alt"
    cid = "+".join(m for m in ("ctrl", "alt", "shift", "cmd") if m in mods) + "+" + key
    if cid in _RESERVED:
        return False, f"reserved for {_RESERVED[cid]}"
    return True, ""


# pynput GlobalHotKeys uses "<ctrl>+<shift>+<space>" style strings.
_PYNPUT_MOD = {"ctrl": "<ctrl>", "shift": "<shift>", "alt": "<alt>", "cmd": "<cmd>", "win": "<cmd>"}
# named keys pynput understands inside <...>; punctuation must be a literal char.
_PYNPUT_KEYNAMES = {"space", "enter", "tab", "esc", "up", "down", "left", "right",
                    "home", "end", "page_up", "page_down", "delete", "insert", "backspace"}
_KEY_CHAR = {"period": ".", "comma": ",", "slash": "/", "semicolon": ";",
             "backslash": "\\", "minus": "-", "equal": "="}


# GNOME keybinding syntax (gsettings), e.g. "<Primary><Shift>space"
_GNOME_MOD = {"ctrl": "<Primary>", "shift": "<Shift>", "alt": "<Alt>",
              "cmd": "<Super>", "win": "<Super>"}


def gnome_binding(mods, key) -> str:
    mods = mods or DEFAULT_MODS
    key = key or DEFAULT_KEY
    return "".join(_GNOME_MOD[m] for m in mods) + key  # keysym name: space/k/period…


def bind_gnome(mods, key, command: str) -> tuple[bool, str]:
    """Register/update a GNOME custom shortcut → `command` for the given chord.
    Returns (ok, info). Used by `crux bind` and auto-run after setup on Linux so a
    changed chord takes effect immediately."""
    import ast
    import shutil
    import subprocess
    if sys.platform != "linux" or not shutil.which("gsettings"):
        return False, "gsettings not available"
    binding = gnome_binding(mods, key)
    base = "org.gnome.settings-daemon.plugins.media-keys"
    path = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/crux/"

    def g(*a):
        return subprocess.run(["gsettings", *a], capture_output=True, text=True)
    try:
        cur = g("get", base, "custom-keybindings").stdout.strip()
        try:
            lst = ast.literal_eval(cur) if cur.startswith("[") else []
        except Exception:
            lst = []
        if path not in lst:
            lst.append(path)
        g("set", base, "custom-keybindings", str(lst))
        child = f"{base}.custom-keybinding:{path}"
        g("set", child, "name", "Capture to CRUX")
        g("set", child, "command", command)
        g("set", child, "binding", binding)
        return True, binding
    except Exception as e:  # pragma: no cover
        return False, str(e)


def pynput_hotkey(mods, key) -> str:
    mods = mods or DEFAULT_MODS
    key = key or DEFAULT_KEY
    parts = [_PYNPUT_MOD[m] for m in mods]
    if key in _PYNPUT_KEYNAMES:
        parts.append(f"<{key}>")
    elif len(key) == 1:
        parts.append(key.lower())
    else:
        parts.append(_KEY_CHAR.get(key, key[0].lower()))  # punctuation → literal char
    return "+".join(parts)


# macOS — Hammerspoon (https://www.hammerspoon.org). Copies the selection first,
# then captures. hs.execute(..., true) runs through a login shell so `crux` is on PATH.
_HAMMERSPOON_TMPL = """\
-- CRUX quick capture — select text anywhere, press {label}.
-- Put this in ~/.hammerspoon/init.lua (or: require it), then reload Hammerspoon.
hs.hotkey.bind({{{mods}}}, "{key}", function()
  hs.eventtap.keyStroke({{"cmd"}}, "c")          -- copy current selection
  hs.timer.doAfter(0.15, function()
    local out, ok = hs.execute("crux capture", true)
    hs.alert.show(ok and "✓ Captured to CRUX" or "CRUX capture failed")
  end)
end)
"""

# macOS — Raycast Script Command. Drop in your Raycast scripts dir, then assign a
# hotkey to it in Raycast → Extensions. (Raycast captures the clipboard; copy first.)
RAYCAST = """\
#!/usr/bin/env bash
# @raycast.schemaVersion 1
# @raycast.title Capture to CRUX
# @raycast.mode silent
# @raycast.packageName CRUX
# @raycast.icon 🧠
crux capture
"""

# Windows — AutoHotkey v1 (https://autohotkey.com). Double-click the .ahk to run.
_AUTOHOTKEY_TMPL = """\
; CRUX quick capture — select text, press {label}.
{chord}::
Send, ^c
Sleep, 150
RunWait, crux capture,, Hide
TrayTip, CRUX, Captured to CRUX, 1
return
"""


def build_snippets(mods=None, key=None) -> dict[str, str]:
    """Build the per-platform hotkey snippets for a chosen chord."""
    mods = mods or DEFAULT_MODS
    key = key or DEFAULT_KEY
    label = chord_label(mods, key)
    hs_mods = ", ".join(f'"{_HS_MOD[m]}"' for m in mods)
    # AHK: "Space" for space, literal char for punctuation, else the letter.
    if key == "space":
        ahk_key = "Space"
    elif key in _KEY_CHAR:
        ahk_key = _KEY_CHAR[key]
    else:
        ahk_key = key if len(key) == 1 else key.capitalize()
    ahk_chord = "".join(_AHK_MOD[m] for m in mods) + ahk_key
    hs_key = _KEY_CHAR.get(key, key)  # Hammerspoon takes "space"/"k"/"." literally
    return {
        "hammerspoon.lua": _HAMMERSPOON_TMPL.format(label=label, mods=hs_mods, key=hs_key),
        "raycast-capture.sh": RAYCAST,
        "crux-capture.ahk": _AUTOHOTKEY_TMPL.format(label=label, chord=ahk_chord),
        "linux-capture.sh": LINUX,
    }

# Linux — grabs the X PRIMARY selection (highlighted text) or clipboard, no Ctrl+C
# needed. Bind it to a key via your DE (GNOME: Settings → Keyboard → Custom Shortcuts)
# or sxhkd. Requires xclip.
LINUX = """\
#!/usr/bin/env sh
# CRUX quick capture — bind this script to a global shortcut.
sel="$(xclip -o -selection primary 2>/dev/null)"
[ -z "$sel" ] && sel="$(xclip -o -selection clipboard 2>/dev/null)"
[ -n "$sel" ] && printf '%s' "$sel" | crux add
"""

SNIPPETS = build_snippets()  # default chord; for back-compat / `crux hotkey`

_INSTRUCTIONS = {
    "darwin": (
        "macOS — two options:\n"
        "  • Hammerspoon (best — captures your selection):\n"
        "      cat {dir}/hammerspoon.lua >> ~/.hammerspoon/init.lua  # then reload Hammerspoon\n"
        "      → press Cmd+Shift+Space after selecting text.\n"
        "  • Raycast: copy {dir}/raycast-capture.sh into your Raycast scripts folder,\n"
        "      then assign a hotkey to “Capture to CRUX” in Raycast → Extensions."
    ),
    "win32": (
        "Windows — AutoHotkey v1 (https://autohotkey.com):\n"
        "  • Double-click {dir}\\crux-capture.ahk (or add it to shell:startup to load on boot).\n"
        "  • Select text, press Ctrl+Shift+Space."
    ),
    "linux": (
        "Linux — requires xclip (sudo apt install xclip):\n"
        "  • chmod +x {dir}/linux-capture.sh\n"
        "  • Bind it to a key: GNOME → Settings → Keyboard → Custom Shortcuts,\n"
        "    command = {dir}/linux-capture.sh  (or add to your sxhkd config).\n"
        "  • Highlight text, press your chosen key."
    ),
}


def _platform() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform  # darwin | win32 | other


def write_snippets(out_dir: Path, mods=None, key=None) -> None:
    """Write the per-platform snippets for a chord (used by the UI setup too)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, body in build_snippets(mods, key).items():
        p = out_dir / name
        p.write_text(body, encoding="utf-8")
        if name.endswith(".sh"):
            p.chmod(0o755)


def run(install: bool, out_dir: Path, mods=None, key=None) -> None:
    plat = _platform()
    if install:
        write_snippets(out_dir, mods, key)
        print(f"✓ wrote hotkey snippets to {out_dir}\n")
    else:
        print("Hotkey snippets (run `crux hotkey --install` to write them out):\n")

    instr = _INSTRUCTIONS.get(plat)
    if instr:
        print(instr.format(dir=out_dir))
    else:
        print(f"Snippets for macOS/Windows/Linux are in {out_dir} — bind one to a global key.")
    print(f"\nDefault chord: {DEFAULT_CHORD} → runs `crux capture` on your selection.")
    print("Make sure `crux` is on PATH for GUI apps (e.g. install with pipx, or use an absolute path).")
