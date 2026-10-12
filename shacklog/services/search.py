"""Full-text search over every synced record (SQLite FTS5).

How it works
------------
* Each model can return ``(title, body)`` from ``search_document()``.
* A flush hook in ``db.py`` calls :func:`index_object` for every new or changed record, so
  the index is always current — including records arriving by sync or import.
* ``search_rows`` maps (entity, entity_id) → integer rowid; ``search_fts`` is the FTS5
  table keyed by that rowid. The mapping makes updates a fast rowid delete+insert instead
  of a full scan.
* The index is local and disposable: :func:`rebuild` recreates it from the data.

If the SQLite build lacks FTS5 (rare), search falls back to ``LIKE`` over the titles in
``search_rows`` — slower and less clever, but it works.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection


def create_schema(conn: Connection) -> bool:
    """Create the search tables. Returns True if FTS5 is available."""
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS search_rows ("
        " rowid INTEGER PRIMARY KEY,"
        " entity TEXT NOT NULL,"
        " entity_id TEXT NOT NULL,"
        " title TEXT,"
        " UNIQUE (entity, entity_id))"
    ))
    try:
        conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5("
            " title, body, tokenize = 'unicode61 remove_diacritics 2')"
        ))
        return True
    except Exception:  # sqlite built without FTS5
        return False


def fts_available(conn: Connection) -> bool:
    row = conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_fts'"
    )).first()
    return row is not None


def index_object(conn: Connection, obj, fts: bool) -> None:
    """Insert, refresh or remove one record in the index."""
    doc = obj.search_document()
    entity = obj.__entity__
    if doc is None:
        return
    if obj.deleted_at is not None:
        remove(conn, entity, obj.id, fts)
        return
    title, body = doc
    conn.execute(
        text("INSERT INTO search_rows (entity, entity_id, title) VALUES (:e, :i, :t) "
             "ON CONFLICT (entity, entity_id) DO UPDATE SET title = excluded.title"),
        {"e": entity, "i": obj.id, "t": title},
    )
    if not fts:
        return
    rowid = conn.execute(
        text("SELECT rowid FROM search_rows WHERE entity = :e AND entity_id = :i"),
        {"e": entity, "i": obj.id},
    ).scalar_one()
    conn.execute(text("DELETE FROM search_fts WHERE rowid = :r"), {"r": rowid})
    conn.execute(
        text("INSERT INTO search_fts (rowid, title, body) VALUES (:r, :t, :b)"),
        {"r": rowid, "t": title or "", "b": body or ""},
    )


def remove(conn: Connection, entity: str, entity_id: str, fts: bool) -> None:
    rowid = conn.execute(
        text("SELECT rowid FROM search_rows WHERE entity = :e AND entity_id = :i"),
        {"e": entity, "i": entity_id},
    ).scalar()
    if rowid is None:
        return
    if fts:
        conn.execute(text("DELETE FROM search_fts WHERE rowid = :r"), {"r": rowid})
    conn.execute(text("DELETE FROM search_rows WHERE rowid = :r"), {"r": rowid})


def rebuild(session) -> int:
    """Drop and rebuild the whole index from the data. Returns the number of entries."""
    from shacklog.models import SYNC_ORDER

    conn = session.connection()
    fts = fts_available(conn)
    conn.execute(text("DELETE FROM search_rows"))
    if fts:
        conn.execute(text("DELETE FROM search_fts"))
    count = 0
    for model in SYNC_ORDER:
        for obj in session.query(model).filter(model.deleted_at.is_(None)).yield_per(500):
            if obj.search_document() is not None:
                index_object(conn, obj, fts)
                count += 1
    return count


@dataclass
class Hit:
    entity: str
    entity_id: str
    title: str
    snippet: str  # HTML-safe text with [[…]] around matches (template turns them into <mark>)
    rank: float


def _fts_query(user_text: str) -> str:
    """Turn free text into a forgiving FTS5 query.

    Every word becomes a prefix term (``ir2u*``) and terms are ANDed, so typing part of a
    callsign or a word finds it. Quotes and operators are stripped: people type callsigns
    like ``IK2-ABC`` or ``F1ZCQ/P`` which FTS5 syntax would otherwise reject.
    """
    words = re.findall(r"[\w.]+", user_text, flags=re.UNICODE)
    return " ".join(f'"{w}"*' for w in words if w)


def query(session, user_text: str, entities: list[str] | None = None, limit: int = 100) -> list[Hit]:
    """Search the index. ``entities`` optionally restricts to some record types."""
    conn = session.connection()
    q = _fts_query(user_text or "")
    if not q:
        return []
    params: dict = {"limit": limit}
    entity_clause = ""
    if entities:
        names = [f":e{i}" for i in range(len(entities))]
        entity_clause = f" AND r.entity IN ({', '.join(names)})"
        params.update({f"e{i}": e for i, e in enumerate(entities)})
    if fts_available(conn):
        params["q"] = q
        rows = conn.execute(text(
            "SELECT r.entity, r.entity_id, r.title,"
            " snippet(search_fts, -1, '[[', ']]', ' … ', 12) AS snip, bm25(search_fts) AS rank"
            " FROM search_fts JOIN search_rows r ON r.rowid = search_fts.rowid"
            f" WHERE search_fts MATCH :q{entity_clause}"
            " ORDER BY rank LIMIT :limit"
        ), params).all()
        return [Hit(r.entity, r.entity_id, r.title or "", r.snip or "", r.rank) for r in rows]
    # Fallback without FTS5: titles only.
    params["like"] = f"%{user_text.strip()}%"
    rows = conn.execute(text(
        "SELECT r.entity, r.entity_id, r.title FROM search_rows r"
        f" WHERE r.title LIKE :like{entity_clause} LIMIT :limit"
    ), params).all()
    return [Hit(r.entity, r.entity_id, r.title or "", "", 0.0) for r in rows]
