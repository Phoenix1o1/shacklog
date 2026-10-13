"""Start ShackLog when you sign in to the computer (desktop mode, no browser window).

One switch, three implementations:

* **Windows** – a value in ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``
  (per user, no admin rights; also visible in Task Manager › Startup apps).
* **Linux** – an XDG autostart file, ``~/.config/autostart/shacklog.desktop``.
* **macOS** – a LaunchAgent, ``~/Library/LaunchAgents/org.shacklog.desktop.plist``.

The program starts with ``--no-browser``: it waits quietly (in the tray on Windows) until
you open it. Only meaningful in desktop mode; the Proxmox hub uses systemd instead.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from shacklog import APP_NAME

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = APP_NAME
MAC_LABEL = "org.shacklog.desktop"


def launch_command() -> list[str]:
    """The command that starts this same program in background desktop mode."""
    if getattr(sys, "frozen", False):  # the built executable
        return [sys.executable, "--no-browser"]
    python = Path(sys.executable)
    if os.name == "nt":
        # pythonw.exe has no console window, like the built executable.
        windowless = python.with_name("pythonw.exe")
        if windowless.exists():
            python = windowless
    return [str(python), "-m", "shacklog", "--no-browser"]


def _quote_windows(args: list[str]) -> str:
    return " ".join(f'"{a}"' if " " in a or not a else a for a in args)


def supported() -> bool:
    return sys.platform in ("win32", "darwin") or sys.platform.startswith("linux")


# --- Windows ---------------------------------------------------------------

def _win_get() -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, VALUE_NAME)[0]
    except OSError:
        return None


def _win_set(enabled: bool) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _quote_windows(launch_command()))
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass


# --- Linux / macOS ---------------------------------------------------------

def linux_file(home: Path | None = None) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (home or Path.home()) / ".config")
    return base / "autostart" / "shacklog.desktop"


def mac_file(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"


def linux_entry() -> str:
    import shlex

    return ("[Desktop Entry]\nType=Application\n"
            f"Name={APP_NAME}\nComment=Amateur radio logbook (background)\n"
            f"Exec={shlex.join(launch_command())}\nTerminal=false\n"
            "X-GNOME-Autostart-enabled=true\n")


def mac_entry() -> str:
    from xml.sax.saxutils import escape

    args = "".join(f"<string>{escape(a)}</string>" for a in launch_command())
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>'
            f"<key>Label</key><string>{MAC_LABEL}</string>"
            f"<key>ProgramArguments</key><array>{args}</array>"
            "<key>RunAtLoad</key><true/></dict></plist>\n")


# --- public API ------------------------------------------------------------

def is_enabled() -> bool:
    if sys.platform == "win32":
        return _win_get() is not None
    if sys.platform == "darwin":
        return mac_file().exists()
    return linux_file().exists()


def set_enabled(enabled: bool) -> None:
    """Turn starting at sign-in on or off. Raises OSError if the system refuses."""
    if sys.platform == "win32":
        _win_set(enabled)
        return
    path, text = ((mac_file(), mac_entry()) if sys.platform == "darwin"
                  else (linux_file(), linux_entry()))
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    elif path.exists():
        path.unlink()
