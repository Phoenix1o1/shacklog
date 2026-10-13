"""Background mode for the desktop: a notification-area (tray) icon instead of a window.

On Windows the executable is built *windowed* (no console). ShackLog then runs in the
background and shows a small icon next to the clock with a menu:

    Open ShackLog            (also a double-click on the icon)
    Open data folder
    Start when I sign in     (tick = on; see services/autostart.py)
    Quit ShackLog            (folds the journal into the database, then exits)

The icon needs ``pystray`` + ``Pillow`` (installed on Windows by requirements.txt). If they
are missing, or on Linux/macOS, :func:`available` is False and the program behaves as
before: a terminal shows the address and Ctrl+C / closing it stops ShackLog.

Running a second copy (double-clicking the exe again) never starts a second server: it
opens the browser on the one already running (see ``cli.running_instance``).
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Callable

from shacklog import APP_NAME

log = logging.getLogger(__name__)

ACCENT = (79, 209, 165, 255)   # --accent of the dark theme
BG = (15, 20, 25, 255)         # --paper of the dark theme


_started_windowed: bool | None = None


def windowed() -> bool:
    """True when the program started without a console (windowed .exe, pythonw.exe).

    Remembered from the first call, so it stays true after :func:`fix_std_streams` has
    attached to a terminal or pointed the streams at the null device.
    """
    global _started_windowed
    if _started_windowed is None:
        _started_windowed = sys.stdout is None or sys.stderr is None
    return _started_windowed


def fix_std_streams() -> None:
    """Give print() and logging somewhere to write when there is no console.

    A command typed in a terminal (``ShackLog.exe where``) attaches to that terminal so its
    output is still visible; otherwise output goes to the null device (the log file in the
    data folder still gets everything).
    """
    if not windowed() or (sys.stdout is not None and sys.stderr is not None):
        return
    if os.name == "nt":
        try:
            if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
                sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
                sys.stderr = sys.stdout
                print()  # the prompt already printed; start on a fresh line
                return
        except Exception:
            pass
    devnull = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = devnull
    if sys.stderr is None:
        sys.stderr = devnull


def message_box(text: str, title: str = APP_NAME) -> None:
    """Show an error when there's no console to show it in (Windows only)."""
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR
            return
        except Exception:
            pass
    print(text, file=sys.stderr)


def available() -> bool:
    if sys.platform != "win32" or os.environ.get("SHACKLOG_NO_TRAY"):
        return False
    try:
        import PIL  # noqa: F401
        import pystray  # noqa: F401
    except ImportError:
        return False
    return True


def icon_image(size: int = 64):
    """The ShackLog icon drawn in code: a mast radiating waves on the dark background."""
    from PIL import Image, ImageDraw

    scale = 4  # draw big, shrink: smooth edges without anti-aliasing support in ImageDraw
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 5, fill=BG)
    cx, top, w = s // 2, int(s * .42), max(2, s // 16)
    # waves above the mast top
    for r in (s * .13, s * .22, s * .31):
        d.arc((cx - r, top - r, cx + r, top + r), 215, 325, fill=ACCENT, width=w)
    # mast with legs
    d.ellipse((cx - w * 1.4, top - w * 1.4, cx + w * 1.4, top + w * 1.4), fill=ACCENT)
    d.line((cx, top, cx - s * .17, s * .88), fill=ACCENT, width=w)
    d.line((cx, top, cx + s * .17, s * .88), fill=ACCENT, width=w)
    d.line((cx - s * .085, s * .66, cx + s * .085, s * .66), fill=ACCENT, width=w)
    return img.resize((size, size), Image.LANCZOS)


def run(url: str, data_dir: Path, shutdown: Callable[[], None], serve: Callable[[], None],
        open_browser: bool) -> None:
    """Serve in a background thread and run the tray icon on this (main) thread."""
    import pystray

    from shacklog.services import autostart, system

    server = threading.Thread(target=serve, name="waitress", daemon=True)
    server.start()

    def open_app(icon=None, item=None):
        webbrowser.open(url)

    def open_data(icon=None, item=None):
        system.open_folder(data_dir)

    def toggle_autostart(icon, item):
        try:
            autostart.set_enabled(not autostart.is_enabled())
        except OSError:
            log.exception("Could not change start at sign-in")

    def quit_app(icon, item):
        icon.visible = False
        icon.stop()
        shutdown()  # exits the process

    menu = pystray.Menu(
        pystray.MenuItem(f"Open {APP_NAME}", open_app, default=True),
        pystray.MenuItem("Open data folder", open_data),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start when I sign in", toggle_autostart,
                         checked=lambda item: autostart.is_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"Quit {APP_NAME}", quit_app),
    )
    icon = pystray.Icon(APP_NAME, icon_image(), f"{APP_NAME} – {url}", menu)

    def ready(icon):
        icon.visible = True
        if open_browser:
            open_app()
        else:
            try:
                icon.notify(f"Running in the background at {url}. "
                            "Right-click this icon for the menu.", APP_NAME)
            except Exception:
                pass

    icon.run(setup=ready)
    shutdown()  # the icon loop ended some other way (e.g. Explorer restarted badly)
