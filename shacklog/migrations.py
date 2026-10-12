"""Schema versions and migrations.

Rules (MANDATES.md §3):

* A **new** database is created from the current models (``Base.metadata.create_all``) and
  stamped with :data:`LATEST`. It never runs old migrations.
* An **existing** database runs every migration above its stored version, in order.
* To change the schema: edit ``models.py``, then add ``_v<N+1>`` below doing the same change
  with SQL on an existing database, register it in :data:`MIGRATIONS` and bump
  :data:`LATEST`. Never edit a migration that has shipped.

Migrations also run on foreign databases before they are imported (bundle.py), so an old
bundle from a laptop that was never updated still imports cleanly.
"""

from __future__ import annotations

from typing import Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection

from shacklog.models import Base
from shacklog.services import search

#: Version a database created by this code has.
LATEST = 1


def _v1_baseline(conn: Connection) -> None:
    """Version 1: the initial schema. Only reached by a pre-release file with version 0."""
    Base.metadata.create_all(conn)
    search.create_schema(conn)


# Example for the future:
# def _v2_add_qso_county(conn: Connection) -> None:
#     conn.execute(text("ALTER TABLE qsos ADD COLUMN county VARCHAR(80)"))

MIGRATIONS: dict[int, Callable[[Connection], None]] = {
    1: _v1_baseline,
}


def get_version(conn: Connection) -> int:
    """Schema version stored in the ``meta`` table (0 if the table doesn't exist yet)."""
    has_meta = conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    )).first()
    if not has_meta:
        return 0
    value = conn.execute(text("SELECT value FROM meta WHERE key='schema_version'")).scalar()
    return int(value) if value else 0


def set_version(conn: Connection, version: int) -> None:
    conn.execute(
        text("INSERT INTO meta (key, value) VALUES ('schema_version', :v) "
             "ON CONFLICT (key) DO UPDATE SET value = excluded.value"),
        {"v": str(version)},
    )


def is_empty(conn: Connection) -> bool:
    """True when the file has no tables at all (brand-new database)."""
    return conn.execute(text("SELECT count(*) FROM sqlite_master WHERE type='table'")).scalar() == 0


def upgrade(conn: Connection) -> tuple[int, int]:
    """Bring the database to :data:`LATEST`. Returns (version before, version after).

    Raises ``RuntimeError`` if the database is newer than this program (it was written by
    a newer ShackLog; opening it could corrupt data, so the user must update first).
    """
    if is_empty(conn):
        Base.metadata.create_all(conn)
        search.create_schema(conn)
        set_version(conn, LATEST)
        return 0, LATEST
    current = get_version(conn)
    if current > LATEST:
        raise RuntimeError(
            f"This database has schema version {current}, but this ShackLog only knows up "
            f"to {LATEST}. Update ShackLog on this computer first."
        )
    for version in range(current + 1, LATEST + 1):
        MIGRATIONS[version](conn)
        set_version(conn, version)
    return current, LATEST
