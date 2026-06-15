"""Global hotkey setup for `crux capture`.

We can't register an OS-level hotkey from Python portably, so CRUX ships
copy-paste-ready snippets for each platform and writes them out with
`crux hotkey --install`. Each snippet copies the current selection, then runs
`crux capture` (which reads the clipboard) so "select anywhere → hotkey → it's in
CRUX" works.
"""

from __future__ import annotations

import sys
from pathlib import Path

DEFAULT_CHORD = "Cmd/Ctrl + Shift + Space"

# macOS — Hammerspoon (https://www.hammerspoon.org). Copies the selection first,
# then captures. hs.execute(..., true) runs through a login shell so `crux` is on PATH.
HAMMERSPOON = """\
-- CRUX quick capture — select text anywhere, press Cmd+Shift+Space.
-- Put this in ~/.hammerspoon/init.lua (or: require it), then reload Hammerspoon.
hs.hotkey.bind({"cmd", "shift"}, "space", function()
  hs.eventtap.keyStroke({"cmd"}, "c")           -- copy current selection
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
AUTOHOTKEY = """\
; CRUX quick capture — select text, press Ctrl+Shift+Space.
^+Space::
Send, ^c
Sleep, 150
RunWait, crux capture,, Hide
TrayTip, CRUX, Captured to CRUX, 1
return
"""

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

SNIPPETS = {
    "hammerspoon.lua": HAMMERSPOON,
    "raycast-capture.sh": RAYCAST,
    "crux-capture.ahk": AUTOHOTKEY,
    "linux-capture.sh": LINUX,
}

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


def run(install: bool, out_dir: Path) -> None:
    plat = _platform()
    if install:
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, body in SNIPPETS.items():
            p = out_dir / name
            p.write_text(body)
            if name.endswith(".sh"):
                p.chmod(0o755)
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
