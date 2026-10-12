"""Backups: online snapshot, daily-once rule from file names, listing and pruning."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from shacklog.models import Qso, utcnow
from shacklog.services import backup


def _touch(folder, when: datetime, reason: str):
    path = folder / f"shacklog-{when:%Y%m%d-%H%M%S}-{reason}.sqlite3"
    path.write_bytes(b"x")
    return path


def test_backup_is_a_complete_database(db, s, tmp_path):
    s.add(Qso(call="IK2BAK", time_on=datetime(2026, 10, 12, 9, 0)))
    s.commit()
    path = backup.create_backup(db, tmp_path / "bk", reason="Before ADIF import!")
    assert path.name.endswith("-before-adif-import.sqlite3")
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT call FROM qsos").fetchall() == [("IK2BAK",)]
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()
    assert not list((tmp_path / "bk").glob(".*partial"))
    # Two in the same second don't overwrite each other.
    second = backup.create_backup(db, tmp_path / "bk", reason="before-adif-import")
    assert second != path and second.exists()
    items = backup.list_backups(tmp_path / "bk")
    assert len(items) == 2 and {i["reason"] for i in items} == {"before-adif-import"}


def test_daily_backup_once_per_utc_day(db, tmp_path):
    folder = tmp_path / "bk"
    first = backup.daily_backup_if_due(db, folder)
    assert first is not None and first.exists()
    assert backup.daily_backup_if_due(db, folder) is None
    # A manual backup today doesn't count as the daily one; yesterday's daily doesn't either.
    folder2 = tmp_path / "bk2"
    folder2.mkdir()
    _touch(folder2, utcnow() - timedelta(days=1), "daily")
    _touch(folder2, utcnow(), "manual")
    assert backup.daily_backup_if_due(db, folder2) is not None


def test_list_ignores_strangers_and_sorts_newest_first(tmp_path):
    base = datetime(2026, 1, 1, 12, 0, 0)
    _touch(tmp_path, base, "daily")
    _touch(tmp_path, base + timedelta(days=2), "manual")
    (tmp_path / "notes.txt").write_text("not a backup")
    (tmp_path / "shacklog-garbage.sqlite3").write_bytes(b"")
    items = backup.list_backups(tmp_path)
    assert [i["reason"] for i in items] == ["manual", "daily"]
    assert items[0]["created"] == base + timedelta(days=2) and items[0]["size"] == 1
    assert backup.resolve_backup(tmp_path, items[0]["name"]) == items[0]["path"]
    assert backup.resolve_backup(tmp_path, "../secret.key") is None
    assert backup.resolve_backup(tmp_path, "notes.txt") is None


def test_prune_keeps_newest_of_each_kind(tmp_path):
    base = datetime(2026, 1, 1)
    for i in range(5):
        _touch(tmp_path, base + timedelta(days=i), "daily")
        _touch(tmp_path, base + timedelta(days=i, hours=1), "before-import")
    backup.prune(tmp_path, keep_daily=2, keep_other=3)
    items = backup.list_backups(tmp_path)
    daily = [i for i in items if i["reason"] == "daily"]
    other = [i for i in items if i["reason"] != "daily"]
    assert len(daily) == 2 and len(other) == 3
    assert daily[0]["created"] == base + timedelta(days=4)
    backup.prune(tmp_path, keep_daily=0, keep_other=0)  # 0 = keep everything
    assert len(backup.list_backups(tmp_path)) == 5
