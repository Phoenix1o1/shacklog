"""Rotating SQLite backups of the database (library blobs are not copied).

Backups use SQLite's online backup API, so they are consistent even while the web server
or the sync worker is writing. Files are named ``shacklog-YYYYmmdd-HHMMSS-<reason>.sqlite3``
(UTC) in ``Paths.backups``; the name alone tells when and why a backup was made, which is
how :func:`daily_backup_if_due` knows whether today's backup exists (no setting needed,
and deleting a backup by hand can't confuse it).

Blobs in ``files/`` are content-addressed and never modified, so they don't need copying:
a backup database plus the blob folder is a complete snapshot.

When backups are made (MANDATES.md §3): before every import (``reason="before-…"``) and
once a day at startup (``reason="daily"``), plus "Back up now" in Settings (``manual``).
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from shacklog.models import utcnow

PREFIX = "shacklog"
SUFFIX = ".sqlite3"
_NAME_RE = re.compile(r"^shacklog-(\d{8})-(\d{6})-([a-z0-9-]+)\.sqlite3$")


def _clean_reason(reason: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", (reason or "manual").lower()).strip("-")
    return cleaned[:40] or "manual"


def copy_database(db, target: Path) -> Path:
    """Consistent snapshot of the live database into ``target`` (a single file, no WAL).

    Written under a temporary name and renamed, so ``target`` is either complete or absent.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name("." + target.name + ".partial")
    partial.unlink(missing_ok=True)
    source = db.raw_connection()
    try:
        dest = sqlite3.connect(str(partial))
        try:
            source.backup(dest)
            # A backup must be a single self-contained file, whatever the source mode.
            dest.execute("PRAGMA journal_mode=DELETE")
        finally:
            dest.close()
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        source.close()
    os.replace(partial, target)
    return target


def create_backup(db, backups_dir: Path, reason: str = "manual") -> Path:
    """Copy the live database to a new file in ``backups_dir`` and return its path."""
    backups_dir = Path(backups_dir)
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    reason = _clean_reason(reason)
    target = backups_dir / f"{PREFIX}-{stamp}-{reason}{SUFFIX}"
    n = 2
    while target.exists():  # two backups in the same second
        target = backups_dir / f"{PREFIX}-{stamp}-{reason}-{n}{SUFFIX}"
        n += 1
    made = copy_database(db, target)
    _mirror(db, made)
    return made


def _mirror(db, made: Path) -> None:
    """Also copy the backup to the folder chosen in Settings › Program, if any."""
    from shacklog.db import get_setting
    from shacklog.services.system import mirror_backup

    try:
        with db.transaction() as s:
            mirror_dir = get_setting(s, "backup_mirror_dir")
            keep = int(get_setting(s, "backup_mirror_keep", 30) or 30)
    except Exception:
        return
    mirror_backup(made, mirror_dir, keep)


def _parse_name(name: str) -> tuple[datetime, str] | None:
    m = _NAME_RE.match(name)
    if not m:
        return None
    try:
        created = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    reason = re.sub(r"-\d+$", "", m.group(3))  # "-2" suffix of same-second backups
    return created, reason


def list_backups(backups_dir: Path) -> list[dict]:
    """Backups found in the folder, newest first: name, path, size, created, reason."""
    backups_dir = Path(backups_dir)
    if not backups_dir.is_dir():
        return []
    items = []
    for path in backups_dir.iterdir():
        parsed = _parse_name(path.name)
        if parsed is None or not path.is_file():
            continue
        created, reason = parsed
        items.append({"name": path.name, "path": path, "size": path.stat().st_size,
                      "created": created, "reason": reason})
    items.sort(key=lambda b: (b["created"], b["name"]), reverse=True)
    return items


def daily_backup_if_due(db, backups_dir: Path) -> Path | None:
    """Make the day's backup unless one with reason "daily" exists for today (UTC)."""
    today = utcnow().date()
    for item in list_backups(backups_dir):
        if item["reason"] == "daily" and item["created"].date() == today:
            return None
    return create_backup(db, backups_dir, reason="daily")


def prune(backups_dir: Path, keep_daily: int = 14, keep_other: int = 20) -> None:
    """Delete old backups: keep the newest ``keep_daily`` daily ones and ``keep_other``
    of every other kind (manual, before imports…). A value below 1 keeps everything."""
    daily = [b for b in list_backups(backups_dir) if b["reason"] == "daily"]
    other = [b for b in list_backups(backups_dir) if b["reason"] != "daily"]
    for group, keep in ((daily, keep_daily), (other, keep_other)):
        if keep is None or keep < 1:
            continue
        for item in group[keep:]:
            try:
                item["path"].unlink()
            except OSError:
                pass  # in use (Windows) or already gone: try again next time


def resolve_backup(backups_dir: Path, name: str) -> Path | None:
    """Path of a backup by file name, refusing anything that isn't a backup in the folder."""
    if _parse_name(name or "") is None or "/" in name or "\\" in name:
        return None
    path = Path(backups_dir) / name
    return path if path.is_file() else None
