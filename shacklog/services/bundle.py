"""Bundles: the whole logbook in one zip, and imports of bundles or foreign database files.

For machines that never reach the hub (PLAN.md §6): export a bundle on one computer,
import it on another. Importing uses the same merge engine as sync (``mode="import"``):
newer versions win and every losing version is kept in ``sync_conflicts``, so an import
never overwrites newer data and never loses older data. Importing the same bundle twice
changes nothing.

Bundle layout (a plain zip; ``manifest.json`` first)::

    manifest.json        app, version, schema_version, instance id/name, created, counts
    database.sqlite3     consistent snapshot (SQLite online backup API)
    files/<sha256>       every library blob referenced by the database

Safety: a backup is made before any import; the foreign database is copied to ``tmp``,
upgraded there with ``migrations.upgrade`` (a database from a *newer* ShackLog is
refused) and read with its own engine, so the original file is never modified. Zip member
names are never used as paths: only ``database.sqlite3`` and ``files/<64 hex>`` are read,
each streamed into a file whose name we choose (no zip-slip).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import uuid
import zipfile

from pathlib import Path
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from shacklog import APP_NAME, __version__, migrations
from shacklog.db import make_engine
from shacklog.models import SYNC_ORDER, ImportLog, StoredFile, utcnow
from shacklog.services import backup, sync_engine
from shacklog.services.filestore import FileStore

log = logging.getLogger(__name__)

MANIFEST = "manifest.json"
DATABASE = "database.sqlite3"
FILES_PREFIX = "files/"
FORMAT = 1
_BLOB_NAME = re.compile(r"^files/([0-9a-f]{64})$")
_SQLITE_MAGIC = b"SQLite format 3\x00"
_BATCH = 500
_COPY_CHUNK = 1024 * 1024


class BundleError(Exception):
    """An import or export that can't proceed; the message is shown to the user."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_sqlite_file(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(16) == _SQLITE_MAGIC
    except OSError:
        return False


def _table_counts(session: Session) -> dict[str, int]:
    return {m.__tablename__: session.execute(select(func.count()).select_from(m)).scalar() or 0
            for m in SYNC_ORDER}


def _referenced_hashes(session: Session) -> list[str]:
    """Every blob a library entry points to, trashed entries included (restorable)."""
    return sorted({h for (h,) in session.execute(select(StoredFile.sha256).distinct()) if h})


def _scratch(paths, suffix: str) -> Path:
    paths.tmp.mkdir(parents=True, exist_ok=True)
    return paths.tmp / f"import-{uuid.uuid4().hex[:12]}{suffix}"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_bundle(db, paths, out_path: Path) -> Path:
    """Write a bundle zip of the database and every referenced library file."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = _scratch(paths, ".sqlite3")
    partial = out_path.with_name("." + out_path.name + ".partial")
    store = FileStore(paths.files)
    try:
        backup.copy_database(db, snapshot)
        # Counts and hashes come from the snapshot, so the manifest matches the zip.
        engine = make_engine(snapshot)
        try:
            with Session(engine) as s:
                counts = _table_counts(s)
                hashes = _referenced_hashes(s)
        finally:
            engine.dispose()
        present = [h for h in hashes if store.exists(h)]
        manifest = {
            "app": APP_NAME, "format": FORMAT, "version": __version__,
            "schema_version": migrations.LATEST,
            "instance_id": db.instance_id, "instance_name": db.instance_name,
            "created": utcnow().isoformat(timespec="seconds"),
            "counts": counts, "files": len(present),
            "files_missing": sorted(set(hashes) - set(present)),
        }
        with zipfile.ZipFile(partial, "w", allowZip64=True) as zf:
            zf.writestr(MANIFEST, json.dumps(manifest, indent=2), zipfile.ZIP_DEFLATED)
            zf.write(snapshot, DATABASE, zipfile.ZIP_DEFLATED)
            for sha in present:
                # Stored, not deflated: manuals, photos and images are compressed already.
                zf.write(store.path_for(sha), FILES_PREFIX + sha, zipfile.ZIP_STORED)
        partial.replace(out_path)
        return out_path
    finally:
        partial.unlink(missing_ok=True)
        snapshot.unlink(missing_ok=True)


def read_manifest(bundle_path: Path) -> dict:
    """The manifest of a bundle (raises BundleError if it isn't one)."""
    try:
        with zipfile.ZipFile(bundle_path) as zf:
            return _manifest(zf)
    except zipfile.BadZipFile:
        raise BundleError("That file isn't a zip, so it isn't a ShackLog bundle.") from None


def _manifest(zf: zipfile.ZipFile) -> dict:
    try:
        manifest = json.loads(zf.read(MANIFEST).decode("utf-8"))
    except KeyError:
        raise BundleError("This zip has no manifest.json: it isn't a ShackLog bundle.") from None
    except ValueError:
        raise BundleError("The bundle's manifest.json is damaged.") from None
    if not isinstance(manifest, dict) or manifest.get("app") != APP_NAME:
        raise BundleError("This zip wasn't made by ShackLog's bundle export.")
    return manifest


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def import_bundle(db, paths, bundle_path: Path) -> dict:
    """Merge a bundle into this database and add its library files. Returns a summary."""
    bundle_path = Path(bundle_path)
    store = FileStore(paths.files)
    extracted = _scratch(paths, ".sqlite3")
    try:
        try:
            zf = zipfile.ZipFile(bundle_path)
        except zipfile.BadZipFile:
            raise BundleError("That file isn't a zip, so it isn't a ShackLog bundle.") from None
        with zf:
            manifest = _manifest(zf)
            schema = manifest.get("schema_version")
            if isinstance(schema, int) and schema > migrations.LATEST:
                raise BundleError(_newer_message(schema))
            try:
                with zf.open(DATABASE) as src, open(extracted, "wb") as dst:
                    shutil.copyfileobj(src, dst, _COPY_CHUNK)
            except KeyError:
                raise BundleError("The bundle has no database.sqlite3 inside.") from None
            summary = _import_database(db, paths, extracted, kind="bundle",
                                       filename=bundle_path.name)
            added = skipped = 0
            damaged: list[str] = []
            for info in zf.infolist():
                m = _BLOB_NAME.match(info.filename)
                if m is None or info.is_dir():
                    continue  # only files/<sha256> members are ever read
                sha = m.group(1)
                if store.exists(sha):
                    skipped += 1
                    continue
                with zf.open(info) as fh:
                    if sync_engine.store_blob_verified(store, fh, sha):
                        added += 1
                    else:
                        damaged.append(sha)
        summary["blobs_added"] = added
        summary["blobs_present"] = skipped
        summary["blobs_damaged"] = damaged
        summary["blobs_missing"] = _missing_blobs(db, store)
        summary["bundle_created"] = manifest.get("created")
        _log_import(db, "bundle", bundle_path.name, summary)
        return summary
    finally:
        _cleanup(extracted)


def import_database_file(db, paths, db_path: Path, files_dir: Path | None = None,
                         look_for_files: bool = True) -> dict:
    """Merge a foreign ShackLog database file.

    Library files are copied from ``files_dir`` — by default the ``files`` folder next to
    the database, as in a ShackLog data folder — when it exists (``look_for_files=False``
    for uploads, which have no folder around them). Files not found that way arrive later
    by sync or with a bundle; the summary lists them under ``blobs_missing``.
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        raise BundleError(f"{db_path.name} doesn't exist.")
    if not is_sqlite_file(db_path):
        raise BundleError(f"{db_path.name} isn't an SQLite database file.")
    copy = _scratch(paths, ".sqlite3")
    try:
        shutil.copyfile(db_path, copy)
        wal = db_path.with_name(db_path.name + "-wal")
        if wal.is_file():  # unflushed changes of a live database
            shutil.copyfile(wal, copy.with_name(copy.name + "-wal"))
        summary = _import_database(db, paths, copy, kind="database", filename=db_path.name)
    finally:
        _cleanup(copy)
    store = FileStore(paths.files)
    source_files = Path(files_dir) if files_dir else db_path.parent / "files"
    added = 0
    if (look_for_files and source_files.is_dir()
            and source_files.resolve() != store.root.resolve()):
        source = FileStore.__new__(FileStore)  # read-only view: don't create folders there
        source.root = source_files
        for sha in _missing_blobs(db, store):
            if source.exists(sha):
                with source.open(sha) as fh:
                    if sync_engine.store_blob_verified(store, fh, sha):
                        added += 1
    summary["blobs_added"] = added
    summary["blobs_missing"] = _missing_blobs(db, store)
    _log_import(db, "database", db_path.name, summary)
    return summary


def _newer_message(schema) -> str:
    return (f"This file comes from a newer ShackLog (database version {schema}; this one "
            f"knows up to {migrations.LATEST}). Update ShackLog here first, then import it.")


def _cleanup(path: Path) -> None:
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm"),
              path.with_name(path.name + "-journal")):
        try:
            p.unlink(missing_ok=True)
        except OSError:  # Windows: still open somewhere; tmp is emptied at startup
            pass


def _missing_blobs(db, store: FileStore) -> list[str]:
    with db.transaction() as s:
        hashes = _referenced_hashes(s)
    return [h for h in hashes if not store.exists(h)]


def _foreign_meta(conn, key: str) -> str | None:
    return conn.execute(text("SELECT value FROM meta WHERE key = :k"), {"k": key}).scalar()


def _import_database(db, paths, foreign: Path, *, kind: str, filename: str) -> dict:
    """Upgrade a scratch copy of a foreign database and merge all its synced rows."""
    if not is_sqlite_file(foreign):
        raise BundleError("The database inside isn't an SQLite file.")
    engine = make_engine(foreign)
    try:
        try:
            with engine.begin() as conn:
                if migrations.is_empty(conn):
                    raise BundleError("That database is empty.")
                before, _after = migrations.upgrade(conn)
                source_id = _foreign_meta(conn, "instance_id")
                source_name = _foreign_meta(conn, "instance_name")
        except RuntimeError as exc:  # newer schema
            version = re.search(r"version (\d+)", str(exc))
            raise BundleError(_newer_message(version.group(1) if version else "?")) from None
        except sqlite3.DatabaseError as exc:
            raise BundleError(f"The database file can't be read ({exc}).") from None
        except Exception as exc:
            if isinstance(exc, BundleError):
                raise
            raise BundleError(f"That doesn't look like a ShackLog database ({exc}).") from None

        if not source_id:
            raise BundleError("That database has no ShackLog instance id: it isn't a ShackLog database.")
        # Backup first (MANDATES.md §3): only now that the file is known to be importable.
        made = backup.create_backup(db, paths.backups, reason="before-import")

        totals = {"inserted": 0, "updated": 0, "unchanged": 0, "conflicts": 0, "ignored": 0,
                  "errors": 0}
        records = 0
        with Session(engine) as foreign_session:
            batch: list[dict] = []
            for record in sync_engine.iter_records(foreign_session, SYNC_ORDER, _BATCH):
                batch.append(record)
                if len(batch) >= _BATCH:
                    records += _merge_batch(db, batch, source_id, source_name, totals)
                    batch = []
            if batch:
                records += _merge_batch(db, batch, source_id, source_name, totals)
    finally:
        engine.dispose()

    summary = dict(totals, kind=kind, filename=filename, records=records,
                   source_instance_id=source_id, source_instance_name=source_name,
                   source_schema=before, backup=made.name,
                   same_instance=source_id == db.instance_id)
    return summary


def _merge_batch(db, batch: list[dict], peer_id: str, peer_name: str | None, totals: dict) -> int:
    # One transaction per batch: memory stays flat on big logs, and an interrupted import
    # simply runs again (merging is idempotent; the backup was made before).
    with db.transaction() as s:
        result = sync_engine.apply_records(s, batch, peer_id, peer_name, mode="import")
    for key in totals:
        totals[key] += result.get(key, 0)
    return len(batch)


def _log_import(db, kind: str, filename: str, summary: dict) -> None:
    """Finish an import: follow merges it brought in, then record it in the import log."""
    from shacklog.services import merge

    with db.transaction() as s:
        summary["merges_followed"] = merge.apply_merges(s).merged
        s.add(ImportLog(kind=kind, filename=filename,
                        summary=json.dumps(summary, default=str, sort_keys=True)))


def default_export_name(db) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", db.instance_name or "instance").strip("-") or "instance"
    return f"{APP_NAME.lower()}-bundle-{safe}-{utcnow():%Y%m%d-%H%M}.zip"


def import_upload(db, paths, path: Path, filename: str) -> dict:
    """Import an uploaded bundle or database file, recognised by content, not extension.
    ``filename`` is the name the user picked (shown in the import history)."""
    path = Path(path)
    if is_sqlite_file(path):
        summary = import_database_file(db, paths, path, look_for_files=False)
    elif zipfile.is_zipfile(path):
        summary = import_bundle(db, paths, path)
    else:
        raise BundleError("That file is neither a ShackLog bundle (.zip) nor a database file "
                          "(.sqlite3). Pick the file made by “Export bundle”.")
    if filename and summary.get("filename") != filename:
        summary["filename"] = filename
        with db.transaction() as s:  # show the user's file name, not the scratch copy's
            entry = s.query(ImportLog).order_by(ImportLog.id.desc()).first()
            if entry is not None:
                entry.filename = filename[:300]
                entry.summary = json.dumps(summary, default=str, sort_keys=True)
    return summary
