"""Database plumbing: engine, sessions, startup, and the automatic change stamping.

The :class:`Database` object is created once per process (by the CLI) and handed to the web
app and the sync worker. It owns the SQLAlchemy engine and knows this instance's identity.

Two session hooks keep the sync and search machinery invisible to the rest of the code:

``before_flush`` — change stamping
    Every new or modified synced record gets the next ``change_seq`` (a per-instance
    counter used by sync to find "what changed since last time"). Local edits also get
    ``updated_at = now`` and ``modified_by_instance = this instance``. Records written by
    the sync engine are marked with ``obj._sync_from = peer_id`` and keep the stamps they
    arrived with.

``after_flush`` — search index
    Changed records are (re)indexed in the FTS5 table (services/search.py).
"""

from __future__ import annotations

import json
import logging
import socket
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from shacklog import migrations
from shacklog.models import Setting, SyncMixin, new_uuid, utcnow
from shacklog.services import geo, search

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SQLite connection setup
# ---------------------------------------------------------------------------

def _sql_geo_dist_km(lat1, lon1, lat2, lon2):
    """SQL function ``geo_dist_km``; NULL if any coordinate is missing."""
    if None in (lat1, lon1, lat2, lon2):
        return None
    return geo.haversine_km(lat1, lon1, lat2, lon2)


def _sql_upper_nospace(value):
    """SQL function ``callnorm``: upper-case, no spaces — for forgiving callsign matches."""
    return value.replace(" ", "").upper() if isinstance(value, str) else value


def configure_sqlite_connection(dbapi_conn: sqlite3.Connection, _record=None) -> None:
    """Pragmas and custom SQL functions, applied to every new connection."""
    cur = dbapi_conn.cursor()
    # WAL lets the web server read while the sync worker writes.
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    # Not enforced on purpose: synced records may arrive before the ones they reference.
    cur.execute("PRAGMA foreign_keys=OFF")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()
    dbapi_conn.create_function("geo_dist_km", 4, _sql_geo_dist_km, deterministic=True)
    dbapi_conn.create_function("callnorm", 1, _sql_upper_nospace, deterministic=True)


def make_engine(path: Path) -> Engine:
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    event.listen(engine, "connect", configure_sqlite_connection)
    return engine


# ---------------------------------------------------------------------------
# The Database object
# ---------------------------------------------------------------------------

class Database:
    """One SQLite file plus this instance's identity."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.engine = make_engine(self.path)
        # Every session carries a pointer back here so the flush hooks can find the
        # instance id and whether FTS5 is available.
        self._sessionmaker = sessionmaker(
            bind=self.engine, expire_on_commit=False, info={"database": self}
        )
        self.instance_id: str = ""
        self.instance_name: str = ""
        self.fts: bool = False

    # -- startup ---------------------------------------------------------

    def init(self) -> "Database":
        """Create or upgrade the schema and load the instance identity. Idempotent."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.engine.begin() as conn:
            before, after = migrations.upgrade(conn)
            if before != after:
                log.info("Database schema %s -> %s at %s", before, after, self.path)
            self.fts = search.fts_available(conn) or search.create_schema(conn)
            # Identity: a UUID that never changes, and a friendly default name.
            self.instance_id = self._meta(conn, "instance_id") or self._set_meta(
                conn, "instance_id", new_uuid())
            self.instance_name = self._meta(conn, "instance_name") or self._set_meta(
                conn, "instance_name", socket.gethostname() or "ShackLog")
            if self._meta(conn, "created_at") is None:
                self._set_meta(conn, "created_at", utcnow().isoformat())
            conn.execute(text("INSERT OR IGNORE INTO seq_counter (id, value) VALUES (1, 0)"))
        return self

    @staticmethod
    def _meta(conn, key: str) -> str | None:
        return conn.execute(text("SELECT value FROM meta WHERE key=:k"), {"k": key}).scalar()

    @staticmethod
    def _set_meta(conn, key: str, value: str) -> str:
        conn.execute(
            text("INSERT INTO meta (key, value) VALUES (:k, :v) "
                 "ON CONFLICT (key) DO UPDATE SET value=excluded.value"),
            {"k": key, "v": value},
        )
        return value

    def rename_instance(self, name: str) -> None:
        with self.engine.begin() as conn:
            self.instance_name = self._set_meta(conn, "instance_name", name.strip() or "ShackLog")

    def meta(self, key: str) -> str | None:
        with self.engine.connect() as conn:
            return self._meta(conn, key)

    # -- sessions --------------------------------------------------------

    def session(self) -> Session:
        """A new ORM session. Caller commits/closes (or use :meth:`transaction`)."""
        return self._sessionmaker()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """``with db.transaction() as s:`` — commits on success, rolls back on error."""
        session = self.session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def current_seq(self) -> int:
        """Highest ``change_seq`` handed out so far."""
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT value FROM seq_counter WHERE id=1")).scalar() or 0

    def raw_connection(self) -> sqlite3.Connection:
        """A plain sqlite3 connection (for the online backup API)."""
        conn = sqlite3.connect(str(self.path), timeout=30)
        configure_sqlite_connection(conn)
        return conn

    def dispose(self) -> None:
        self.engine.dispose()


# ---------------------------------------------------------------------------
# Settings (local key/value, JSON encoded)
# ---------------------------------------------------------------------------

def get_setting(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(Setting, key)
    if row is None or row.value is None:
        return default
    try:
        return json.loads(row.value)
    except ValueError:
        return row.value


def set_setting(session: Session, key: str, value: Any) -> None:
    row = session.get(Setting, key)
    encoded = json.dumps(value)
    if row is None:
        session.add(Setting(key=key, value=encoded))
    else:
        row.value = encoded


# ---------------------------------------------------------------------------
# Flush hooks
# ---------------------------------------------------------------------------

@event.listens_for(SyncMixin, "init", propagate=True)
def _assign_id_on_create(target, args, kwargs):
    """Give new objects their UUID immediately, so links can be built before a flush."""
    if kwargs.get("id") is None:
        target.id = new_uuid()


@event.listens_for(Session, "before_flush")
def _stamp_changes(session: Session, _flush_context, _instances) -> None:
    database: Database | None = session.info.get("database")
    if database is None:
        return
    changed = [o for o in session.new if isinstance(o, SyncMixin)]
    changed += [
        o for o in session.dirty
        if isinstance(o, SyncMixin) and session.is_modified(o, include_collections=False)
    ]
    if not changed:
        return
    # Reserve a block of sequence numbers atomically. UPDATE…RETURNING takes the SQLite
    # write lock, so two writers can never get the same numbers.
    top = session.connection().execute(
        text("UPDATE seq_counter SET value = value + :n WHERE id = 1 RETURNING value"),
        {"n": len(changed)},
    ).scalar_one()
    seq = top - len(changed)
    now = utcnow()
    for obj in changed:
        seq += 1
        obj.change_seq = seq
        peer = getattr(obj, "_sync_from", None)
        if peer is not None:
            # Arrived from another instance: keep its stamps, remember where it came from.
            obj.received_from = peer
            del obj._sync_from
            continue
        # A local change.
        if obj.created_at is None:
            obj.created_at = now
        if obj.origin_instance is None:
            obj.origin_instance = database.instance_id
        obj.updated_at = now
        obj.modified_by_instance = database.instance_id
        obj.received_from = None


@event.listens_for(Session, "after_flush")
def _update_search_index(session: Session, _flush_context) -> None:
    database: Database | None = session.info.get("database")
    if database is None:
        return
    conn = session.connection()
    for obj in list(session.new) + list(session.dirty):
        if isinstance(obj, SyncMixin):
            search.index_object(conn, obj, database.fts)
