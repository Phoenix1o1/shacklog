"""Table-agnostic serialisation and merging of synced records (no HTTP, no Flask).

Sync (hub and clients), bundle imports and database imports all move records through this
module, so there is exactly one implementation of the merge rule of PLAN.md §6.

Records on the wire
-------------------
A record is ``{"table": "qsos", "row": {...}}``. ``row`` holds every column of the model
except :data:`shacklog.models.LOCAL_COLUMNS`; datetimes and dates are ISO 8601 strings,
``None`` stays ``None``. Tables and columns this version doesn't know (sent by a newer
peer) are ignored, never fatal.

Merge rule (PLAN.md §6)
-----------------------
Versions are compared by ``(updated_at, modified_by_instance)``; identical versions are
skipped. Otherwise the mode decides whether the two sides changed the record
*concurrently*:

``hub``     the client sends the hub ``change_seq`` it last pulled (``client_last_pull``).
            The hub row changed concurrently if its ``change_seq`` is above that and the
            hub's version did not itself come from this client.
``client``  the hub already resolved conflicts, so its version applies — unless the local
            row was changed here (not received from the hub) after our last push
            (``change_seq > local_dirty_after``).
``import``  no history: treated as concurrent, unless the local row is exactly what this
            same source sent us last time (``received_from`` is the source and nothing
            was edited here since): then the source simply has a newer version.

Not concurrent → the incoming version applies. Concurrent → the newer version wins and,
when the data differs (ignoring the two stamp columns), the loser is stored in
``sync_conflicts`` so it can be restored. Nothing is ever discarded.

Writing: incoming values are set on the ORM object and ``obj._sync_from = peer_id`` tells
the flush hook in ``db.py`` to keep the incoming stamps and to remember ``received_from``.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Iterator

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, inspect, select, text
from sqlalchemy.orm import Session, undefer

from shacklog.models import (
    LOCAL_COLUMNS,
    SYNC_ORDER,
    TABLES,
    StoredFile,
    SyncConflict,
    SyncMixin,
    utcnow,
)
from shacklog.services.filestore import is_sha256

log = logging.getLogger(__name__)

#: Version stamps: ignored when deciding whether two versions hold different data.
STAMP_COLUMNS = frozenset({"updated_at", "modified_by_instance"})
#: Columns a restore must not take from the losing snapshot (identity and stamps).
_RESTORE_SKIP = STAMP_COLUMNS | {"id", "created_at", "origin_instance"}
MODES = ("hub", "client", "import")
_LOAD_CHUNK = 500  # ids per IN (...) query; well under SQLite's variable limit


# ---------------------------------------------------------------------------
# Columns and value conversion
# ---------------------------------------------------------------------------

_COLUMNS: dict[type, dict[str, Any]] = {}


def synced_columns(model: type[SyncMixin]) -> dict[str, Any]:
    """``{attribute name: column type}`` of the columns that travel between instances."""
    cols = _COLUMNS.get(model)
    if cols is None:
        cols = {attr.key: attr.columns[0].type for attr in inspect(model).column_attrs
                if attr.key not in LOCAL_COLUMNS}
        _COLUMNS[model] = cols
    return cols


def to_json_value(value: Any) -> Any:
    """Python value → JSON-safe value (datetimes and dates as ISO 8601)."""
    if isinstance(value, (datetime, date)):  # datetime is a date subclass: same treatment
        return value.isoformat()
    return value


def from_json_value(value: Any, coltype: Any) -> Any:
    """JSON value → Python value for a column type. Raises ValueError/TypeError if unreadable."""
    if value is None:
        return None
    if isinstance(coltype, DateTime):
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).strip().replace("Z", "+00:00"))
        if dt.tzinfo is not None:  # storage convention: naive UTC
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    if isinstance(coltype, Date):
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value).strip()[:10])
    if isinstance(coltype, Boolean):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(coltype, Integer):
        return int(value)
    if isinstance(coltype, Float):
        return float(value)
    if isinstance(value, (dict, list)):  # a newer peer sending structure in a text column
        return json.dumps(value)
    return value


def serialize(obj: SyncMixin) -> dict:
    """JSON-safe row of a synced object; :data:`LOCAL_COLUMNS` excluded."""
    return {key: to_json_value(getattr(obj, key)) for key in synced_columns(type(obj))}


def record_for(obj: SyncMixin) -> dict:
    """The wire format: ``{"table": …, "row": {…}}``."""
    return {"table": obj.__tablename__, "row": serialize(obj)}


def _version(updated_at: datetime | None, by: str | None) -> tuple[datetime, str]:
    return (updated_at or datetime.min, by or "")


def _chunks(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ---------------------------------------------------------------------------
# What changed since a watermark
# ---------------------------------------------------------------------------

@dataclass
class ChangesPage:
    records: list[dict]
    #: Watermark to store: every change up to here was sent or deliberately skipped.
    last_seq: int
    #: True when the page was cut short (limit or size); ask again from ``last_seq``.
    more: bool


def changes_page(session: Session, seq: int, exclude_peer: str | None = None,
                 limit: int = 500, max_bytes: int | None = None) -> ChangesPage:
    """Records of every synced table with ``change_seq > seq``, in ``change_seq`` order.

    Rows whose ``received_from`` equals ``exclude_peer`` are skipped (they came from the
    peer we are talking to), but the watermark still moves past them so paging ends.
    ``max_bytes`` caps the JSON size of a page (library files carry their extracted text),
    always letting at least one record through.
    """
    limit = max(1, int(limit))
    seq = int(seq or 0)
    # Read the counter *before* scanning and only look at rows up to it. A writer reserves
    # its numbers while holding SQLite's write lock and keeps it until commit, so every
    # row numbered up to this ceiling is already committed and visible to the scans below;
    # rows above it are left for the next call. That makes it safe to move the watermark
    # to the ceiling once nothing is left (past numbers superseded by later edits).
    ceiling = session.execute(text("SELECT value FROM seq_counter WHERE id = 1")).scalar() or 0

    candidates: list[tuple[int, type[SyncMixin], str, str | None]] = []
    for model in SYNC_ORDER:
        rows = session.execute(
            select(model.id, model.change_seq, model.received_from)
            .where(model.change_seq > seq, model.change_seq <= ceiling)
            .order_by(model.change_seq).limit(limit + 1)
        ).all()
        candidates.extend((r.change_seq, model, r.id, r.received_from) for r in rows)
    candidates.sort(key=lambda c: c[0])
    truncated = len(candidates) > limit
    candidates = candidates[:limit]

    wanted: dict[type[SyncMixin], list[str]] = defaultdict(list)
    for _cs, model, rid, received_from in candidates:
        if exclude_peer is None or received_from != exclude_peer:
            wanted[model].append(rid)
    loaded: dict[tuple[type, str], SyncMixin] = {}
    for model, ids in wanted.items():
        for chunk in _chunks(ids, _LOAD_CHUNK):
            for obj in session.query(model).options(undefer("*")).filter(model.id.in_(chunk)):
                loaded[(model, obj.id)] = obj

    records: list[dict] = []
    size = 0
    last = seq
    for change_seq, model, rid, _rf in candidates:
        obj = loaded.get((model, rid))
        if obj is not None:
            rec = record_for(obj)
            if max_bytes:
                n = len(json.dumps(rec))
                if records and size + n > max_bytes:
                    truncated = True
                    break
                size += n
            records.append(rec)
        last = change_seq
    if not truncated:
        last = max(last, ceiling)
    return ChangesPage(records, last, truncated)


def current_seq(session: Session) -> int:
    """Highest ``change_seq`` handed out, read through this session."""
    return session.execute(text("SELECT value FROM seq_counter WHERE id = 1")).scalar() or 0


def changes_since(session: Session, seq: int, exclude_peer: str | None,
                  limit: int) -> tuple[list[dict], int]:
    """Contract form of :func:`changes_page`: ``(records, last_seq)``."""
    page = changes_page(session, seq, exclude_peer, limit)
    return page.records, page.last_seq


# ---------------------------------------------------------------------------
# Applying incoming records
# ---------------------------------------------------------------------------

def _parse_row(model: type[SyncMixin], row: dict) -> dict[str, Any]:
    cols = synced_columns(model)
    parsed = {k: from_json_value(v, cols[k]) for k, v in row.items() if k in cols}
    if model is StoredFile and parsed.get("sha256") and not is_sha256(parsed["sha256"]):
        raise ValueError("sha256 is not a SHA-256 hash")  # stored as a conflict, not applied
    return parsed


def _snapshot_of_incoming(row: dict, parsed: dict[str, Any]) -> dict:
    """Incoming row as stored in a conflict: normalised values, unknown columns kept."""
    snap = dict(row)
    snap.update({k: to_json_value(v) for k, v in parsed.items()})
    return snap


_REQUIRED: dict[type, frozenset[str]] = {}


def _required_columns(model: type[SyncMixin]) -> frozenset[str]:
    """Columns declared NOT NULL here (a peer with another schema might send None)."""
    req = _REQUIRED.get(model)
    if req is None:
        req = frozenset(a.key for a in inspect(model).column_attrs
                        if not a.columns[0].nullable)
        _REQUIRED[model] = req
    return req


def _write(obj: SyncMixin, values: dict[str, Any], peer_id: str) -> None:
    required = _required_columns(type(obj))
    for key, value in values.items():
        if key == "id" or (value is None and key in required):
            continue  # a NULL there would abort the whole batch; keep ours / the default
        setattr(obj, key, value)
    obj._sync_from = peer_id  # consumed by db._stamp_changes at flush


def _save_conflict(session: Session, table: str, record_id: str, kind: str, loser: dict,
                   winner: dict | None, peer_id: str | None, peer_name: str | None) -> bool:
    """Store a losing version unless the very same one is already stored (re-imports)."""
    loser_json = json.dumps(loser, sort_keys=True, default=str)
    exists = session.query(SyncConflict.id).filter(
        SyncConflict.table_name == table, SyncConflict.record_id == record_id,
        SyncConflict.loser_snapshot == loser_json).first()
    if exists:
        return False
    session.add(SyncConflict(
        table_name=table, record_id=record_id, peer_id=peer_id, peer_name=peer_name,
        detected_at=utcnow(), kind=kind, loser_snapshot=loser_json,
        winner_snapshot=json.dumps(winner, sort_keys=True, default=str) if winner else None,
    ))
    return True


def apply_records(session: Session, records: list[dict], peer_id: str, peer_name: str | None,
                  *, mode: str, client_last_pull: int | None = None,
                  local_dirty_after: int | None = None) -> dict:
    """Merge incoming records into this database (see the module docstring for the rule).

    Returns counts: ``inserted``, ``updated``, ``unchanged``, ``conflicts`` (losers saved),
    ``ignored`` (unknown tables, malformed records) and ``errors`` (unreadable values; the
    raw record is kept as a conflict so it is not lost). The caller commits.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    stats = {"inserted": 0, "updated": 0, "unchanged": 0, "conflicts": 0, "ignored": 0,
             "errors": 0}
    by_model: dict[type[SyncMixin], list[dict]] = defaultdict(list)
    for rec in records or []:
        model = TABLES.get(rec.get("table")) if isinstance(rec, dict) else None
        row = rec.get("row") if model is not None else None
        if model is None or not isinstance(row, dict) or not row.get("id"):
            stats["ignored"] += 1
            continue
        by_model[model].append(row)

    for model in SYNC_ORDER:  # parents first, as the plan promises
        rows = by_model.get(model)
        if not rows:
            continue
        existing: dict[str, SyncMixin] = {}
        ids = list({str(r["id"]) for r in rows})
        for chunk in _chunks(ids, _LOAD_CHUNK):
            for obj in session.query(model).options(undefer("*")).filter(model.id.in_(chunk)):
                existing[obj.id] = obj
        for row in rows:
            _merge_one(session, model, row, existing, peer_id, peer_name, mode,
                       client_last_pull, local_dirty_after, stats)

    session.flush()
    # An object that ended up unmodified keeps its marker; drop it so a later local edit
    # in this session isn't mistaken for a synced one.
    for obj in list(session.identity_map.values()):
        obj.__dict__.pop("_sync_from", None)
    return stats


def _merge_one(session: Session, model: type[SyncMixin], row: dict,
               existing: dict[str, SyncMixin], peer_id: str, peer_name: str | None, mode: str,
               client_last_pull: int | None, local_dirty_after: int | None,
               stats: dict) -> None:
    table = model.__tablename__
    record_id = str(row["id"])
    try:
        incoming = _parse_row(model, row)
    except (TypeError, ValueError) as exc:
        log.warning("Unreadable %s record %s from %s: %s", table, record_id, peer_name or peer_id, exc)
        local = existing.get(record_id)
        _save_conflict(session, table, record_id, "incoming_discarded", dict(row),
                       serialize(local) if local is not None else None, peer_id, peer_name)
        stats["errors"] += 1
        return
    incoming["id"] = record_id

    local = existing.get(record_id)
    if local is None:
        obj = model(id=record_id)
        _write(obj, incoming, peer_id)
        session.add(obj)
        existing[record_id] = obj
        stats["inserted"] += 1
        return

    local_v = _version(local.updated_at, local.modified_by_instance)
    incoming_v = _version(incoming.get("updated_at", local.updated_at),
                          incoming.get("modified_by_instance", local.modified_by_instance))
    if local_v == incoming_v:
        stats["unchanged"] += 1
        return

    differs = any(getattr(local, k) != v for k, v in incoming.items()
                  if k not in STAMP_COLUMNS and k != "id")
    incoming_newer = incoming_v > local_v

    if mode == "hub":
        # The client had seen our version if it pulled past it, or if it wrote it.
        concurrent = (local.change_seq or 0) > (client_last_pull or 0) and local.received_from != peer_id
    elif mode == "client":
        # Changed here (by an edit or an import) after our last push, and not by the hub.
        concurrent = local.received_from != peer_id and (local.change_seq or 0) > (local_dirty_after or 0)
    else:
        # A row received from this source and not edited here since is just an older
        # copy of the source's own record: its newer version applies without a conflict.
        concurrent = not (local.received_from == peer_id and incoming_v > local_v)

    if not concurrent:
        _write(local, incoming, peer_id)
        stats["updated"] += 1
        return
    if not differs:  # same data, different stamps: keep the newer stamps, nothing to save
        if incoming_newer:
            _write(local, incoming, peer_id)
            stats["updated"] += 1
        else:
            stats["unchanged"] += 1
        return
    if incoming_newer:
        loser = serialize(local)
        _write(local, incoming, peer_id)
        if _save_conflict(session, table, record_id, "local_overwritten", loser,
                          _snapshot_of_incoming(row, incoming), peer_id, peer_name):
            stats["conflicts"] += 1
        stats["updated"] += 1
    else:
        if _save_conflict(session, table, record_id, "incoming_discarded",
                          _snapshot_of_incoming(row, incoming), serialize(local), peer_id, peer_name):
            stats["conflicts"] += 1
        stats["unchanged"] += 1


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------

def restore_conflict(session: Session, conflict_id: int) -> None:
    """Bring back the losing version as a normal local edit, so it syncs everywhere."""
    conflict = session.get(SyncConflict, conflict_id)
    if conflict is None:
        raise LookupError(f"No conflict {conflict_id}")
    model = TABLES.get(conflict.table_name)
    if model is None:
        raise ValueError(f"This version of ShackLog doesn't know the table {conflict.table_name}")
    snapshot = json.loads(conflict.loser_snapshot)
    cols = synced_columns(model)
    values = {k: from_json_value(v, cols[k]) for k, v in snapshot.items()
              if k in cols and k not in _RESTORE_SKIP}
    obj = session.get(model, conflict.record_id)
    if obj is None:
        obj = model(id=conflict.record_id)
        for key in ("created_at", "origin_instance"):
            if key in snapshot:
                setattr(obj, key, from_json_value(snapshot[key], cols[key]))
        session.add(obj)
    else:
        # The version being replaced is kept too (already resolved, but restorable), so
        # restoring never loses data either: "the Conflicts page can restore either".
        current = serialize(obj)
        if any(current.get(k) != to_json_value(v) for k, v in values.items()):
            session.add(SyncConflict(
                table_name=conflict.table_name, record_id=conflict.record_id,
                peer_id=None, peer_name="replaced by a restore", detected_at=utcnow(),
                kind="local_overwritten", loser_snapshot=json.dumps(current, sort_keys=True),
                winner_snapshot=conflict.loser_snapshot, resolved_at=utcnow(),
                resolution="replaced"))
    for key, value in values.items():
        setattr(obj, key, value)
    conflict.resolved_at = utcnow()
    conflict.resolution = "restored"
    session.flush()


def dismiss_conflict(session: Session, conflict_id: int) -> None:
    """Mark a conflict as seen; the losing version stays stored."""
    conflict = session.get(SyncConflict, conflict_id)
    if conflict is None:
        raise LookupError(f"No conflict {conflict_id}")
    conflict.resolved_at = utcnow()
    conflict.resolution = "dismissed"
    session.flush()


@dataclass
class ConflictView:
    """What the Conflicts page needs to show one conflict."""

    conflict: SyncConflict
    model: type[SyncMixin] | None
    current: SyncMixin | None
    loser: dict
    #: (field, value in the losing version, value in the current record)
    rows: list[tuple[str, Any, Any]]


def describe_conflict(session: Session, conflict: SyncConflict) -> ConflictView:
    """Fields where the stored loser differs from the record as it is now."""
    try:
        loser = json.loads(conflict.loser_snapshot)
    except ValueError:
        loser = {}
    model = TABLES.get(conflict.table_name)
    current = session.get(model, conflict.record_id) if model is not None else None
    now = serialize(current) if current is not None else {}
    keys = [k for k in (synced_columns(model) if model else loser) if k != "id"]
    keys += [k for k in loser if k not in keys and k != "id"]
    rows = [(k, loser.get(k), now.get(k)) for k in keys if loser.get(k) != now.get(k)]
    # Show the version stamps first: they say which edit is which.
    rows.sort(key=lambda r: (r[0] not in STAMP_COLUMNS, keys.index(r[0])))
    return ConflictView(conflict, model, current, loser, rows)


def store_blob_verified(store, stream, expected_sha256: str) -> bool:
    """Store a blob received from elsewhere only if its bytes hash to ``expected_sha256``.

    Unlike ``FileStore.put_verified`` (which keeps mismatching bytes under their real hash),
    a damaged or wrong upload leaves nothing behind. Written to a temp file next to the
    store, then renamed, so a crash never leaves a half-written blob under a valid name.
    """
    import hashlib
    import os
    import tempfile

    if store.exists(expected_sha256):
        while stream.read(1024 * 1024):  # drain: callers may reuse the connection
            pass
        return True
    digest = hashlib.sha256()
    fd, tmp_name = tempfile.mkstemp(dir=store.root, prefix=".incoming-")
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != expected_sha256:
            return False
        target = store.path_for(expected_sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_name, target)
        return True
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def iter_records(session: Session, models: Iterable[type[SyncMixin]] = SYNC_ORDER,
                 batch: int = 500) -> Iterator[dict]:
    """Every row of the given tables as wire records (used by database imports)."""
    for model in models:
        query = session.query(model).options(undefer("*")).order_by(model.id)
        for obj in query.yield_per(batch):
            yield record_for(obj)
