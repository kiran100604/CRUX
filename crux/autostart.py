"""Run CRUX as a background service that starts on login — no terminal to babysit.

The core capture/inject loop needs no daemon (agents spawn CRUX on demand, the
popup writes straight to the DB). What *does* want a persistent process is the
always-on dashboard and, where supported, the global capture hotkey. This module
installs a detached, auto-starting login service so the user never runs a command
in a terminal again:

  • macOS   → a LaunchAgent (~/Library/LaunchAgents), runs `crux app` at login
  • Linux   → a systemd *user* service if available, else an XDG autostart entry
  • Windows → a Startup-folder shortcut running `pythonw` (windowless)

Everything here is best-effort and reversible: `enable` / `disable` are idempotent
and never raise into the caller — they return {ok, enabled, method, path, message}.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.crux.agent"        # macOS launchd label
UNIT = "crux.service"           # systemd user unit name


def _default_home() -> Path:
    return (Path.home() / ".crux").resolve()


def _crux_argv(cfg, subcmd: str) -> list[str]:
    """Absolute command the service runs. Login shells often have a bare PATH, so
    prefer the resolved `crux` entry point and fall back to `python -m crux.cli`."""
    crux = shutil.which("crux")
    base = [crux] if crux else [sys.executable, "-m", "crux.cli"]
    return base + [subcmd, "--no-open"]


def _service_argv(cfg) -> list[str]:
    """macOS/Windows run the tray appliance (dashboard + hotkey + tray). Linux runs
    the dashboard server; its capture hotkey is bound at the OS level (gsettings),
    so it doesn't need the pynput listener that's unreliable on Wayland."""
    if sys.platform == "linux":
        return _crux_argv(cfg, "start")
    return _crux_argv(cfg, "app")


def _env(cfg) -> dict:
    """Carry CRUX_HOME only when it's non-default, so a custom home still resolves
    under the login session (which won't have the user's shell env)."""
    home = Path(cfg.home).resolve()
    return {"CRUX_HOME": str(home)} if home != _default_home() else {}


# --------------------------------------------------------------------------- #
# macOS — LaunchAgent
# --------------------------------------------------------------------------- #
def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _macos_enable(cfg) -> dict:
    argv = _service_argv(cfg)
    log = str(Path(cfg.home) / "agent.log")
    args_xml = "".join(f"    <string>{a}</string>\n" for a in argv)
    env = _env(cfg)
    env_xml = ""
    if env:
        env_xml = ("  <key>EnvironmentVariables</key>\n  <dict>\n"
                   + "".join(f"    <key>{k}</key><string>{v}</string>\n" for k, v in env.items())
                   + "  </dict>\n")
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f'  <key>Label</key><string>{LABEL}</string>\n'
        f'  <key>ProgramArguments</key>\n  <array>\n{args_xml}  </array>\n'
        f'{env_xml}'
        '  <key>RunAtLoad</key><true/>\n'
        '  <key>KeepAlive</key><true/>\n'
        f'  <key>StandardOutPath</key><string>{log}</string>\n'
        f'  <key>StandardErrorPath</key><string>{log}</string>\n'
        '</dict>\n</plist>\n'
    )
    p = _macos_plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(plist, encoding="utf-8")
    # reload so it starts now (unload first — ignore errors if not loaded)
    subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
    subprocess.run(["launchctl", "load", "-w", str(p)], capture_output=True)
    return {"ok": True, "enabled": True, "method": "launchd", "path": str(p),
            "message": "CRUX will start automatically when you log in."}


def _macos_disable(cfg) -> dict:
    p = _macos_plist_path()
    if p.exists():
        subprocess.run(["launchctl", "unload", "-w", str(p)], capture_output=True)
        p.unlink()
    return {"ok": True, "enabled": False, "method": "launchd", "path": str(p),
            "message": "CRUX will no longer start on login."}


def _macos_status(cfg) -> dict:
    p = _macos_plist_path()
    return {"enabled": p.exists(), "method": "launchd", "path": str(p)}


# --------------------------------------------------------------------------- #
# Linux — systemd user service (preferred) → XDG autostart (fallback)
# --------------------------------------------------------------------------- #
def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT


def _xdg_autostart_path() -> Path:
    return Path.home() / ".config" / "autostart" / "crux.desktop"


def _has_systemd_user() -> bool:
    """True only when a user systemd manager is actually running — writing a unit is
    pointless otherwise. `show-environment` needs a live user manager, so it's a
    reliable probe (unlike `--version`, which succeeds with no manager)."""
    if not shutil.which("systemctl"):
        return False
    try:
        r = subprocess.run(["systemctl", "--user", "show-environment"],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _linux_enable(cfg) -> dict:
    argv = _service_argv(cfg)
    exec_line = " ".join(_shquote(a) for a in argv)
    env = _env(cfg)
    if _has_systemd_user():
        env_lines = "".join(f"Environment={k}={v}\n" for k, v in env.items())
        unit = (
            "[Unit]\n"
            "Description=CRUX local context dashboard\n"
            "After=default.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"{env_lines}"
            f"ExecStart={exec_line}\n"
            "Restart=on-failure\n"
            "RestartSec=3\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        p = _systemd_unit_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(unit, encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        r = subprocess.run(["systemctl", "--user", "enable", "--now", UNIT],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return {"ok": True, "enabled": True, "method": "systemd", "path": str(p),
                    "message": "CRUX runs on login (systemd user service). "
                               "Tip: `loginctl enable-linger` keeps it alive when logged out."}
        # systemd present but enable failed → fall through to XDG so the user still
        # gets autostart; clean up the unit we wrote.
        p.unlink(missing_ok=True)
    # XDG autostart fallback (no systemd, or enable failed)
    env_prefix = ("env " + " ".join(f"{k}={_shquote(v)}" for k, v in env.items()) + " ") if env else ""
    desktop = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=CRUX\n"
        "Comment=Local context layer for AI coding agents\n"
        f"Exec={env_prefix}{exec_line}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        "Categories=Utility;\n"
    )
    p = _xdg_autostart_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(desktop, encoding="utf-8")
    return {"ok": True, "enabled": True, "method": "xdg-autostart", "path": str(p),
            "message": "CRUX will start when you next log in (desktop autostart)."}


def _linux_disable(cfg) -> dict:
    removed = []
    up = _systemd_unit_path()
    if up.exists():
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT], capture_output=True)
        up.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        removed.append(str(up))
    xp = _xdg_autostart_path()
    if xp.exists():
        xp.unlink(missing_ok=True)
        removed.append(str(xp))
    return {"ok": True, "enabled": False, "method": "linux", "path": (removed[0] if removed else ""),
            "message": "CRUX will no longer start on login."}


def _linux_status(cfg) -> dict:
    up, xp = _systemd_unit_path(), _xdg_autostart_path()
    if up.exists():
        return {"enabled": True, "method": "systemd", "path": str(up)}
    if xp.exists():
        return {"enabled": True, "method": "xdg-autostart", "path": str(xp)}
    return {"enabled": False, "method": "systemd", "path": str(up)}


# --------------------------------------------------------------------------- #
# Windows — Startup-folder shortcut (pythonw = no console window)
# --------------------------------------------------------------------------- #
def _win_startup_lnk() -> Path:
    import os
    return (Path(os.environ.get("APPDATA", Path.home()))
            / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "CRUX.lnk")


def _win_pythonw() -> str:
    cand = Path(sys.executable).with_name("pythonw.exe")
    return str(cand) if cand.exists() else sys.executable


def _win_enable(cfg) -> dict:
    lnk = _win_startup_lnk()
    lnk.parent.mkdir(parents=True, exist_ok=True)
    target = _win_pythonw()
    args = "-m crux.cli app --no-open"
    ico = Path(cfg.home) / "crux.ico"
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
        "$s.TargetPath='{target}';$s.Arguments='{args}';"
        "$s.Description='CRUX (starts on login)';{icon}$s.Save()"
    ).format(lnk=str(lnk), target=target, args=args,
             icon=(f"$s.IconLocation='{ico}';" if ico.exists() else ""))
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=15, check=True)
    except Exception as e:
        return {"ok": False, "enabled": False, "method": "startup", "path": str(lnk),
                "message": f"Couldn't create the Startup shortcut ({e})."}
    return {"ok": True, "enabled": True, "method": "startup", "path": str(lnk),
            "message": "CRUX will start automatically when you sign in."}


def _win_disable(cfg) -> dict:
    lnk = _win_startup_lnk()
    if lnk.exists():
        lnk.unlink()
    return {"ok": True, "enabled": False, "method": "startup", "path": str(lnk),
            "message": "CRUX will no longer start on login."}


def _win_status(cfg) -> dict:
    lnk = _win_startup_lnk()
    return {"enabled": lnk.exists(), "method": "startup", "path": str(lnk)}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def _shquote(arg: str) -> str:
    return f'"{arg}"' if (" " in arg and not arg.startswith('"')) else arg


def enable_autostart(cfg) -> dict:
    try:
        if sys.platform == "darwin":
            return _macos_enable(cfg)
        if sys.platform == "win32":
            return _win_enable(cfg)
        if sys.platform.startswith("linux"):
            return _linux_enable(cfg)
    except Exception as e:  # never let autostart wiring crash setup
        return {"ok": False, "enabled": False, "method": "", "path": "",
                "message": f"Couldn't enable autostart ({str(e)[:120]})."}
    return {"ok": False, "enabled": False, "method": "", "path": "",
            "message": f"Autostart isn't supported on {sys.platform} yet."}


def disable_autostart(cfg) -> dict:
    try:
        if sys.platform == "darwin":
            return _macos_disable(cfg)
        if sys.platform == "win32":
            return _win_disable(cfg)
        if sys.platform.startswith("linux"):
            return _linux_disable(cfg)
    except Exception as e:
        return {"ok": False, "enabled": True, "method": "", "path": "",
                "message": f"Couldn't disable autostart ({str(e)[:120]})."}
    return {"ok": True, "enabled": False, "method": "", "path": "", "message": ""}


def autostart_status(cfg) -> dict:
    try:
        if sys.platform == "darwin":
            return _macos_status(cfg)
        if sys.platform == "win32":
            return _win_status(cfg)
        if sys.platform.startswith("linux"):
            return _linux_status(cfg)
    except Exception:
        pass
    return {"enabled": False, "method": "", "path": ""}
