"""Automatic updates: check GitHub for a newer version, install it when allowed.

Two situations, one status file (``<data>/control/update.json``) that the web pages read:

**PC (desktop mode, git working copy)** — at every start, if the computer is online and
*Update automatically at start* is on (default), ShackLog runs ``git fetch``. If GitHub has
something newer it hands over to the update helper (``packaging/update_windows.bat`` /
``update_unix.sh``: pull, tests, rebuild, restart) and exits before serving. Offline, or
when the check fails, it simply starts. A failed update is not retried for the same
version for :data:`RETRY_AFTER_H` hours, so a broken build can't cause a restart loop.
An executable older than its source (the code was pulled but the build failed) is
rebuilt the same way, under the same guard.

**Hub (Proxmox, headless)** — the web service runs unprivileged and can't touch the code,
so root does the work: a systemd timer runs ``shacklog-ctl check`` a few minutes after
boot and every 6 hours. It fetches, writes the same status file, and installs the update
(with automatic rollback) only if *Install updates automatically* is on — a flag file
``<data>/control/auto-update`` that the web page creates or removes.

The status file is JSON::

    {"checked_at": "2026-10-13T04:00:00Z", "current": "b2507d2", "latest": "c0ffee1",
     "behind": 2, "log": ["c0ffee1 Fix…", "…"], "error": "", "source": "desktop"}
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

RETRY_AFTER_H = 12
FETCH_TIMEOUT_S = 20


def control_dir(data_dir: Path) -> Path:
    from shacklog.config import Paths

    return Paths(Path(data_dir)).control


def status_file(data_dir: Path) -> Path:
    return control_dir(data_dir) / "update.json"


def auto_flag(data_dir: Path) -> Path:
    """Hub: present = install updates automatically (read by shacklog-ctl, run as root)."""
    return control_dir(data_dir) / "auto-update"


def read_status(data_dir: Path) -> dict:
    try:
        data = json.loads(status_file(data_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_status(data_dir: Path, **fields) -> dict:
    fields.setdefault("checked_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    target = status_file(data_dir)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(fields, indent=1), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        log.warning("Couldn't write %s", target)
    return fields


def hub_auto_install(data_dir: Path) -> bool:
    return auto_flag(data_dir).exists()


def set_hub_auto_install(data_dir: Path, enabled: bool) -> None:
    flag = auto_flag(data_dir)
    if enabled:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("on\n", encoding="utf-8")
    elif flag.exists():
        flag.unlink()


# ---------------------------------------------------------------------------
# Desktop
# ---------------------------------------------------------------------------

def check_now(data_dir: Path, timeout: int = FETCH_TIMEOUT_S) -> dict:
    """Fetch from GitHub and record what's new. Never raises; never prompts for a login."""
    from shacklog.services import system

    status = system.repo_status(check_remote=True, fetch_timeout=timeout)
    if not status.updatable:
        return write_status(data_dir, source="desktop", current=status.commit, behind=None,
                            error="Not a git working copy, or git isn't installed.")
    latest, new_log = status.commit, []
    if status.behind:
        _, latest = system._git(status.path, "rev-parse", "--short", "@{u}")
        _, out = system._git(status.path, "log", "--oneline", "--no-decorate", "HEAD..@{u}")
        new_log = out.splitlines()[:30]
    return write_status(data_dir, source="desktop", current=status.commit, latest=latest,
                        behind=status.behind, log=new_log, error=status.error)


def _attempt_file(data_dir: Path) -> Path:
    return control_dir(data_dir) / "update-attempt.json"


def recently_attempted(data_dir: Path, target: str) -> bool:
    try:
        last = json.loads(_attempt_file(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (last.get("target") == target
            and time.time() - float(last.get("at", 0)) < RETRY_AFTER_H * 3600)


def exe_outdated(repo: Path | None) -> str:
    """The commit to rebuild for if the running executable is older than its source.

    Happens when an update pulled the code but the tests or the build failed: git is then
    up to date, yet the program is still the old one. Returns "" when the exe matches, or
    when this isn't a built executable with a known commit.
    """
    from shacklog.services import system

    built = system.build_commit()
    if not built or repo is None:
        return ""
    code, head = system._git(repo, "rev-parse", "HEAD")
    return head if code == 0 and head and head != built else ""


def update_at_launch(data_dir: Path, enabled: bool,
                     relaunch_args: list[str] | None = None) -> bool:
    """Desktop start-up: True if an update was launched (the caller must exit now).

    ``relaunch_args`` are passed to the ShackLog the update helper starts afterwards.
    """
    from shacklog.services import system

    if not enabled or os.environ.get("SHACKLOG_NO_AUTO_UPDATE"):
        return False
    status = system.repo_status()
    if not status.updatable or status.dirty or status.branch in ("", "HEAD"):
        return False  # downloaded exe, no git, hand-edited files or detached: leave it
    info = check_now(data_dir)
    behind, target = info.get("behind"), info.get("latest") or ""
    stale = exe_outdated(status.path)
    if not behind and not stale:
        return False
    if not behind:
        target = stale[:7]
    if recently_attempted(data_dir, target):
        log.warning("Update to %s was tried recently and didn't finish; starting as is.", target)
        return False
    try:
        _attempt_file(data_dir).write_text(json.dumps({"target": target, "at": time.time()}),
                                           encoding="utf-8")
    except OSError:
        return False  # without the guard a broken update could loop: don't risk it
    if behind:
        log.info("Update available (%s new change%s): updating before start.",
                 behind, "" if behind == 1 else "s")
    else:
        log.info("The program is older than its source (%s): rebuilding before start.", target)
    system.launch_desktop_update(status.path, rebuild=bool(stale), relaunch_args=relaunch_args)
    return True
