"""Bundles and database-file imports: export → import reproduces data, twice is a no-op."""

from __future__ import annotations

import json
import shutil
import sqlite3
import zipfile
from datetime import datetime, timedelta

import pytest

from shacklog import migrations
from shacklog.models import (
    SYNC_ORDER,
    Equipment,
    ImportLog,
    Qso,
    StoredFile,
    SyncConflict,
)
from shacklog.services import backup, bundle
from tests.conftest import build_app


@pytest.fixture
def two(tmp_path):
    src_app, src_db, src_paths = build_app(tmp_path / "laptop")
    dst_app, dst_db, dst_paths = build_app(tmp_path / "desktop")
    src_db.rename_instance("Laptop")
    yield (src_app, src_db, src_paths), (dst_app, dst_db, dst_paths)
    src_db.dispose()
    dst_db.dispose()


def _counts(db) -> dict[str, int]:
    with db.transaction() as s:
        return {m.__tablename__: s.query(m).count() for m in SYNC_ORDER}


def _fill(app, db):
    store = app.config["FILESTORE"]
    sha, size = store.put_bytes(b"%PDF TM-733E service manual")
    sha2, size2 = store.put_bytes(b"trashed photo")
    with db.transaction() as s:
        s.add(Qso(call="IK2BUN", time_on=datetime(2026, 10, 1, 8, 0), name="Bundle"))
        s.add(Equipment(name="TM-733E", kind="radio"))
        s.add(StoredFile(sha256=sha, filename="tm733.pdf", size=size, category="manual"))
        gone = StoredFile(sha256=sha2, filename="old.jpg", size=size2, category="photo")
        s.add(gone)
        s.flush()
        gone.soft_delete()
    return sha, sha2


def test_export_then_import_reproduces_counts_and_blobs(two, tmp_path):
    (src_app, src_db, src_paths), (dst_app, dst_db, dst_paths) = two
    sha, sha2 = _fill(src_app, src_db)
    out = bundle.export_bundle(src_db, src_paths, tmp_path / "out" / "laptop.zip")
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        manifest = json.loads(zf.read("manifest.json"))
    assert names[0] == "manifest.json" and "database.sqlite3" in names
    assert f"files/{sha}" in names and f"files/{sha2}" in names  # trashed files too
    assert manifest["instance_name"] == "Laptop" and manifest["counts"]["qsos"] == 1
    assert manifest["schema_version"] == migrations.LATEST and manifest["files"] == 2
    assert not list(src_paths.tmp.iterdir())  # scratch snapshot cleaned up

    before = _counts(dst_db)
    summary = bundle.import_bundle(dst_db, dst_paths, out)
    after = _counts(dst_db)
    src = _counts(src_db)
    # The desktop had its own two stations; everything from the laptop was added.
    assert after["stations"] == before["stations"] + src["stations"]
    for table in ("qsos", "equipment", "files"):
        assert after[table] == src[table]
    assert summary["inserted"] == sum(src.values()) and summary["conflicts"] == 0
    assert summary["blobs_added"] == 2 and summary["blobs_missing"] == []
    assert summary["source_instance_name"] == "Laptop"
    store = dst_app.config["FILESTORE"]
    assert store.read_bytes(sha) == b"%PDF TM-733E service manual"
    with dst_db.transaction() as s:
        q = s.query(Qso).one()
        assert q.received_from == src_db.instance_id and q.origin_instance == src_db.instance_id
        log = s.query(ImportLog).one()
        assert log.kind == "bundle" and json.loads(log.summary)["inserted"] == summary["inserted"]
    # A backup was made before the import.
    assert any(b["reason"] == "before-import" for b in backup.list_backups(dst_paths.backups))

    # Importing the same bundle again changes nothing.
    seq = dst_db.current_seq()
    again = bundle.import_bundle(dst_db, dst_paths, out)
    assert again["inserted"] == again["updated"] == again["conflicts"] == 0
    assert again["unchanged"] == summary["inserted"]
    assert _counts(dst_db) == after and dst_db.current_seq() == seq


def test_import_keeps_newer_local_edits_and_saves_losers(two, tmp_path):
    (src_app, src_db, src_paths), (dst_app, dst_db, dst_paths) = two
    _fill(src_app, src_db)
    out = bundle.export_bundle(src_db, src_paths, tmp_path / "a.zip")
    bundle.import_bundle(dst_db, dst_paths, out)
    with dst_db.transaction() as s:
        s.query(Qso).one().name = "Edited on desktop"  # newer than the laptop's version
    summary = bundle.import_bundle(dst_db, dst_paths, out)
    assert summary["conflicts"] == 1
    with dst_db.transaction() as s:
        assert s.query(Qso).one().name == "Edited on desktop"
        c = s.query(SyncConflict).one()
        assert c.kind == "incoming_discarded" and '"Bundle"' in c.loser_snapshot
    # Same bundle a third time: the conflict isn't stored twice.
    bundle.import_bundle(dst_db, dst_paths, out)
    with dst_db.transaction() as s:
        assert s.query(SyncConflict).count() == 1


def test_import_database_file_with_files_folder(two):
    (src_app, src_db, src_paths), (dst_app, dst_db, dst_paths) = two
    sha, _ = _fill(src_app, src_db)
    # A raw data folder (e.g. copied from an old laptop's disk), live WAL included.
    summary = bundle.import_database_file(dst_db, dst_paths, src_paths.database)
    assert summary["kind"] == "database" and summary["blobs_added"] == 2
    assert dst_app.config["FILESTORE"].exists(sha)
    assert _counts(dst_db)["qsos"] == 1
    assert src_paths.database.exists()  # the original is untouched


def test_import_upload_detects_type_and_uses_given_name(two, tmp_path):
    (src_app, src_db, src_paths), (dst_app, dst_db, dst_paths) = two
    _fill(src_app, src_db)
    snap = backup.copy_database(src_db, tmp_path / "upload.bin")
    summary = bundle.import_upload(dst_db, dst_paths, snap, "laptop-copy.sqlite3")
    assert summary["kind"] == "database" and summary["blobs_added"] == 0
    assert len(summary["blobs_missing"]) == 2  # they'll come with sync or a bundle
    with dst_db.transaction() as s:
        assert s.query(ImportLog).one().filename == "laptop-copy.sqlite3"
    junk = tmp_path / "junk.txt"
    junk.write_text("hello")
    with pytest.raises(bundle.BundleError, match="neither"):
        bundle.import_upload(dst_db, dst_paths, junk, "junk.txt")


def test_newer_schema_is_refused(two, tmp_path):
    (src_app, src_db, src_paths), (dst_app, dst_db, dst_paths) = two
    future = backup.copy_database(src_db, tmp_path / "future.sqlite3")
    conn = sqlite3.connect(future)
    conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'",
                 (str(migrations.LATEST + 1),))
    conn.commit()
    conn.close()
    before = _counts(dst_db)
    with pytest.raises(bundle.BundleError, match="newer ShackLog"):
        bundle.import_database_file(dst_db, dst_paths, future)
    assert _counts(dst_db) == before
    # The same refusal through a bundle manifest.
    zpath = tmp_path / "future.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"app": "ShackLog", "schema_version": 99}))
        zf.write(future, "database.sqlite3")
    with pytest.raises(bundle.BundleError, match="newer ShackLog"):
        bundle.import_bundle(dst_db, dst_paths, zpath)
    assert not [p for p in dst_paths.tmp.iterdir()]  # scratch copies removed


def test_zip_slip_and_bad_blobs_are_ignored(two, tmp_path):
    (src_app, src_db, src_paths), (dst_app, dst_db, dst_paths) = two
    sha, _ = _fill(src_app, src_db)
    good = bundle.export_bundle(src_db, src_paths, tmp_path / "good.zip")
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(evil, "w") as dst:
        for info in src.infolist():
            data = src.read(info)
            if info.filename == f"files/{sha}":
                data = b"tampered bytes"
            dst.writestr(info.filename, data)
        dst.writestr("../../escape.txt", b"pwned")
        dst.writestr("files/../../escape2.txt", b"pwned")
    summary = bundle.import_bundle(dst_db, dst_paths, evil)
    assert sha in summary["blobs_damaged"] and sha in summary["blobs_missing"]
    assert not dst_app.config["FILESTORE"].exists(sha)
    assert not (tmp_path / "escape.txt").exists()
    assert not list(tmp_path.rglob("escape*.txt"))


def test_not_a_bundle(two, tmp_path):
    (_, _, _), (_, dst_db, dst_paths) = two
    z = tmp_path / "other.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("readme.txt", "hi")
    with pytest.raises(bundle.BundleError, match="manifest"):
        bundle.import_bundle(dst_db, dst_paths, z)
    with pytest.raises(bundle.BundleError):
        bundle.import_database_file(dst_db, dst_paths, z)


def test_old_bundle_rows_lose_to_newer_hub_rows_regardless_of_order(two, tmp_path):
    """An old laptop bundle imported late never overwrites newer data."""
    (src_app, src_db, src_paths), (dst_app, dst_db, dst_paths) = two
    _fill(src_app, src_db)
    old_bundle = bundle.export_bundle(src_db, src_paths, tmp_path / "old.zip")
    with src_db.transaction() as s:
        s.query(Qso).one().name = "Newer"
    new_bundle = bundle.export_bundle(src_db, src_paths, tmp_path / "new.zip")
    bundle.import_bundle(dst_db, dst_paths, new_bundle)
    bundle.import_bundle(dst_db, dst_paths, old_bundle)
    with dst_db.transaction() as s:
        assert s.query(Qso).one().name == "Newer"
        assert s.query(SyncConflict).filter_by(kind="incoming_discarded").count() == 1


def test_newer_bundle_from_same_source_updates_without_conflict(two, tmp_path):
    """Importing a later bundle of the same instance just brings its newer versions."""
    (src_app, src_db, src_paths), (dst_app, dst_db, dst_paths) = two
    _fill(src_app, src_db)
    bundle.import_bundle(dst_db, dst_paths, bundle.export_bundle(src_db, src_paths, tmp_path / "1.zip"))
    with src_db.transaction() as s:
        s.query(Qso).one().name = "Second"
    bundle.import_bundle(dst_db, dst_paths, bundle.export_bundle(src_db, src_paths, tmp_path / "2.zip"))
    with dst_db.transaction() as s:
        assert s.query(Qso).one().name == "Second"
        assert s.query(SyncConflict).count() == 0


def test_bundle_carries_no_secrets(tmp_path):
    """Password hash, sync tokens and the callbook password stay on this instance."""
    import sqlite3
    import zipfile

    from shacklog.db import set_setting
    from shacklog.services import bundle
    from tests.conftest import build_app

    app, db, paths = build_app(tmp_path / "src")
    with db.transaction() as s:
        for key in ("password_hash", "sync_hub_token", "sync_client_token", "callbook_password"):
            set_setting(s, key, f"SECRET-{key}")
        set_setting(s, "page_size", 100)
    out = bundle.export_bundle(db, paths, tmp_path / "b.zip")
    with zipfile.ZipFile(out) as zf:
        raw = zf.read(bundle.DATABASE)
    assert b"SECRET-" not in raw
    (tmp_path / "x.sqlite3").write_bytes(raw)
    from contextlib import closing

    with closing(sqlite3.connect(tmp_path / "x.sqlite3")) as conn:
        assert conn.execute("SELECT value FROM settings WHERE key='page_size'").fetchone()
    with db.transaction() as s:  # the live database still has them
        from shacklog.db import get_setting

        assert get_setting(s, "sync_hub_token") == "SECRET-sync_hub_token"
    db.dispose()
