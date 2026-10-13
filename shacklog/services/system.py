"""Controlling the running program: clean shutdown, updates from git, server requests.

Three concerns that differ between a PC and the Proxmox hub:

Clean shutdown
    SQLite keeps recent changes in ``shacklog.sqlite3-wal`` until a *checkpoint* copies
    them into the main file. Closing the console window on Windows kills the process
    without one, which leaves a tiny main file and a big ``-wal`` file next to it (no
    data is lost, but it is confusing and makes copying the database risky). We
    checkpoint periodically, at startup and on every way the program can be stopped.

Updates from git
    A *source install* is a git working copy: on a PC the executable is built into
    ``<repo>/dist``, on the hub the service runs from ``/opt/shacklog``. Updating means
    ``git pull`` + reinstall/rebuild + restart. The running program can't replace its own
    executable, so it launches a helper script and exits:
    ``packaging/update_windows.bat`` or ``packaging/update_unix.sh``.

Server requests (hub)
    The web service runs as an unprivileged user and cannot call ``systemctl``. Buttons
    in *Settings › Program* write ``<data>/control/request`` (``update``, ``restart`` or
    ``stop``); a systemd *path unit* installed by ``deploy/`` notices the file and runs
    ``shacklog-ctl handle-request`` as root.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

CHECKPOINT_INTERVAL_S = 300
SERVER_REQUESTS = ("update", "restart", "stop", "check")


# ---------------------------------------------------------------------------
# Clean shutdown
# ---------------------------------------------------------------------------

def checkpoint(db) -> None:
    """Fold the WAL journal back into the main database file (best effort)."""
    try:
        with db.engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:  # busy with a long read: the next checkpoint will catch up
        log.debug("Checkpoint skipped", exc_info=True)


def start_checkpoint_timer(db, interval: float = CHECKPOINT_INTERVAL_S,
                           hourly: Callable[[], None] | None = None) -> threading.Event:
    """Checkpoint every ``interval`` seconds in a daemon thread. Set the event to stop.

    ``hourly`` (if given) also runs about once an hour in the same thread: the daily
    backup check, so a hub or a PC that stays on for weeks still backs up every day.
    """
    stop = threading.Event()
    every = max(1, round(3600 / interval))

    def loop() -> None:
        ticks = 0
        while not stop.wait(interval):
            checkpoint(db)
            ticks += 1
            if hourly is not None and ticks % every == 0:
                try:
                    hourly()
                except Exception:
                    log.exception("Hourly task failed")

    threading.Thread(target=loop, name="shacklog-checkpoint", daemon=True).start()
    return stop


_exit_callbacks: list[Callable[[], None]] = []
_handler_ref = None  # keep the ctypes callback alive


def on_exit(callback: Callable[[], None]) -> None:
    """Run ``callback`` when the console closes, the user logs off, or SIGTERM arrives."""
    _exit_callbacks.append(callback)
    if len(_exit_callbacks) == 1:
        _install_handlers()


def _run_exit_callbacks() -> None:
    while _exit_callbacks:
        callback = _exit_callbacks.pop()
        try:
            callback()
        except Exception:
            log.exception("Exit callback failed")


def _install_handlers() -> None:
    def on_signal(_signum, _frame):
        _run_exit_callbacks()
        os._exit(0)

    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, on_signal)
            except (ValueError, OSError):  # not the main thread / unsupported
                pass
    if os.name == "nt":
        _install_windows_console_handler()


def _install_windows_console_handler() -> None:
    """Closing the console window sends CTRL_CLOSE_EVENT; Windows then gives the process
    about 5 seconds before killing it — enough for a checkpoint."""
    global _handler_ref
    try:
        import ctypes
        from ctypes import wintypes

        handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def handler(event: int) -> bool:
            # 0 Ctrl+C, 1 Ctrl+Break, 2 close, 5 logoff, 6 shutdown
            if event in (2, 5, 6):
                _run_exit_callbacks()
                return False  # let Windows finish closing
            return False

        _handler_ref = handler_type(handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_handler_ref, True)
    except Exception:
        log.debug("Console handler not installed", exc_info=True)


# ---------------------------------------------------------------------------
# Opening the data folder (desktop)
# ---------------------------------------------------------------------------

def open_folder(path: Path) -> bool:
    """Show a folder in Explorer / Finder / the file manager."""
    try:
        if os.name == "nt":
            os.startfile(str(path))  # noqa: S606 (local path we own)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except Exception:
        log.exception("Couldn't open %s", path)
        return False


# ---------------------------------------------------------------------------
# Git working copy
# ---------------------------------------------------------------------------

def repo_root() -> Path | None:
    """The git working copy this program runs from, if any.

    Frozen executable: ``<repo>/dist/ShackLog.exe`` → ``<repo>``.
    From source: the folder containing the ``shacklog`` package.
    """
    if getattr(sys, "frozen", False):
        start = Path(sys.executable).resolve().parent
    else:
        start = Path(__file__).resolve().parents[2]
    for candidate in (start, *start.parents[:2]):
        if (candidate / ".git").exists() and (candidate / "shacklog").is_dir():
            return candidate
    return None


def git_executable() -> str | None:
    found = shutil.which("git")
    if found:
        return found
    if os.name == "nt":  # Git for Windows not on PATH for GUI-launched programs
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("LOCALAPPDATA", "")):
            exe = Path(base) / ("Programs/Git" if "Local" in base else "Git") / "cmd/git.exe"
            if exe.is_file():
                return str(exe)
    return None


def _git(repo: Path, *args: str, timeout: int = 60) -> tuple[int, str]:
    git = git_executable()
    if git is None:
        return 127, "git is not installed"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # Never wait for a login prompt (terminal or Git Credential Manager window): a check
    # at start-up must fail fast instead.
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    try:
        done = subprocess.run([git, "-C", str(repo), *args], capture_output=True, text=True,
                              timeout=timeout, creationflags=flags, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


@dataclass
class RepoStatus:
    path: Path | None
    git: bool
    branch: str = ""
    commit: str = ""
    commit_date: str = ""
    behind: int | None = None  # None = unknown (not checked / offline)
    dirty: bool = False
    error: str = ""

    @property
    def updatable(self) -> bool:
        return self.path is not None and self.git


def repo_status(check_remote: bool = False, fetch_timeout: int = 60) -> RepoStatus:
    path = repo_root()
    status = RepoStatus(path=path, git=git_executable() is not None)
    if path is None or not status.git:
        return status
    _, status.branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    _, status.commit = _git(path, "rev-parse", "--short", "HEAD")
    _, status.commit_date = _git(path, "log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M")
    code, out = _git(path, "status", "--porcelain", "--untracked-files=no")
    status.dirty = code == 0 and bool(out)
    if check_remote:
        code, out = _git(path, "fetch", "--quiet", "origin", timeout=fetch_timeout)
        if code != 0:
            status.error = out[-400:] or "git fetch failed"
        else:
            code, out = _git(path, "rev-list", "--count", "HEAD..@{u}")
            status.behind = int(out) if code == 0 and out.isdigit() else None
    return status


def launch_desktop_update(repo: Path) -> None:
    """Start the update helper detached; it waits for this process to exit."""
    pid = str(os.getpid())
    if os.name == "nt":
        script = repo / "packaging" / "update_windows.bat"
        flags = subprocess.CREATE_NEW_CONSOLE  # its own window, visible progress
        subprocess.Popen(["cmd.exe", "/c", str(script), pid], cwd=str(repo), creationflags=flags)
    else:
        script = repo / "packaging" / "update_unix.sh"
        subprocess.Popen(["bash", str(script), pid], cwd=str(repo), start_new_session=True)


# ---------------------------------------------------------------------------
# Server requests (hub)
# ---------------------------------------------------------------------------

def control_available() -> bool:
    """True when systemd watches the request file (set by deploy/shacklog.service)."""
    return os.environ.get("SHACKLOG_CONTROL") == "systemd"


def request_file(data_dir: Path) -> Path:
    return data_dir / "control" / "request"


def write_server_request(data_dir: Path, action: str) -> None:
    if action not in SERVER_REQUESTS:
        raise ValueError(action)
    target = request_file(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(action + "\n", encoding="utf-8")
    os.replace(tmp, target)  # atomic, so the path unit sees a complete file


def last_control_log(data_dir: Path, lines: int = 40) -> str:
    log_path = data_dir / "control" / "last.log"
    try:
        return "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Backup mirror
# ---------------------------------------------------------------------------

def mirror_backup(backup_path: Path, mirror_dir: str | None, keep: int = 30) -> Path | None:
    """Copy a backup to a second folder (e.g. a OneDrive or NAS folder) and prune it.

    The live database never goes there, only finished backup copies, so a syncing
    cloud folder can't corrupt it.
    """
    if not mirror_dir:
        return None
    target_dir = Path(mirror_dir).expanduser()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / backup_path.name
        shutil.copy2(backup_path, target)
        copies = sorted(target_dir.glob("shacklog-*.sqlite3"), key=lambda p: p.name, reverse=True)
        for old in copies[keep:]:
            old.unlink(missing_ok=True)
        return target
    except OSError:
        log.exception("Couldn't copy the backup to %s", target_dir)
        return None


# ---------------------------------------------------------------------------
# Network addresses (shown in the footer and Settings › Program)
# ---------------------------------------------------------------------------

_addr_cache: tuple[float, list[str]] = (0.0, [])


def local_addresses() -> list[str]:
    """This machine's IPv4 addresses on the network (no loopback), main one first.

    The main one is the address of the route to the internet (a UDP "connect" sends
    nothing); the rest come from the host name. Cached for a minute.
    """
    global _addr_cache
    import socket
    import time

    now = time.monotonic()
    if now - _addr_cache[0] < 60:
        return _addr_cache[1]
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 53))  # UDP connect only picks a route: nothing is sent
            found.append(sock.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    result = []
    for ip in found:
        if ip and not ip.startswith("127.") and ip != "0.0.0.0" and ip not in result:
            result.append(ip)
    _addr_cache = (now, result)
    return result
