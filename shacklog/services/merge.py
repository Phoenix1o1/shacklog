"""Finding and merging duplicate records.

Duplicates appear when two instances each create "the same" thing before they sync — for
example the hub's setup creates a *Home* station and the PC already had one. A merge
keeps one record (the *target*) and folds the others (*sources*) into it:

1. every reference to a source is pointed at the target — foreign-key columns found
   automatically from the models (QSO.station_id, RadioConfig.radio_id, …), generic
   :class:`~shacklog.models.Link` rows, and this instance's settings (default station…);
2. empty fields of the target are filled from the source (nothing typed is overwritten);
3. the source is soft-deleted (it stays in the Trash and can be restored).

Everything is an ordinary local edit, so the result syncs to every other instance and
the duplicate disappears everywhere. Pure data logic: no Flask imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from shacklog.db import get_setting, set_setting
from shacklog.models import (
    SYNC_ORDER,
    Contact,
    Equipment,
    Link,
    Repeater,
    Station,
    StoredFile,
    SyncMixin,
    utcnow,
)

#: Types the duplicate finder looks at, in page order.
MERGEABLE: list[type[SyncMixin]] = [Station, Equipment, Repeater, Contact, StoredFile]

#: Local settings that hold a record id, per type.
SETTING_REFS = {Station: ("default_station_id", "reference_station_id")}

#: Bookkeeping columns never copied between records.
_SKIP = {"id", "created_at", "updated_at", "deleted_at", "origin_instance",
         "modified_by_instance", "change_seq", "received_from"}


def references_to(model: type[SyncMixin]) -> list[tuple[type[SyncMixin], str]]:
    """(model, column) pairs whose foreign key points at ``model``'s table."""
    table = model.__table__
    refs = []
    for other in SYNC_ORDER:
        for column in other.__table__.columns:
            for fk in column.foreign_keys:
                if fk.column.table is table:
                    refs.append((other, column.name))
    return refs


@dataclass
class MergeResult:
    target_id: str
    merged: int = 0
    references: int = 0
    links: int = 0
    filled: list[str] = field(default_factory=list)


def merge(session: Session, model: type[SyncMixin], target_id: str,
          source_ids: Iterable[str]) -> MergeResult:
    """Fold ``source_ids`` into ``target_id``. The caller commits."""
    target = session.get(model, target_id)
    if target is None or target.deleted_at is not None:
        raise ValueError("The record to keep doesn't exist (or is in the trash).")
    result = MergeResult(target_id=target_id)
    entity = model.__entity__
    for source_id in source_ids:
        if source_id == target_id:
            continue
        source = session.get(model, source_id)
        if source is None or source.deleted_at is not None:
            continue
        # 1. foreign keys
        for ref_model, column in references_to(model):
            col = getattr(ref_model, column)
            for row in session.query(ref_model).filter(col == source_id):
                setattr(row, column, target_id)
                result.references += 1
        # 2. generic links; a link that would now join the target to itself, or repeat an
        #    existing link, is removed instead.
        existing = {
            (l.a_type, l.a_id, l.b_type, l.b_id)
            for l in session.query(Link).filter(Link.deleted_at.is_(None))
        }
        for link in session.query(Link).filter(Link.deleted_at.is_(None)):
            changed = False
            if link.a_type == entity and link.a_id == source_id:
                link.a_id, changed = target_id, True
            if link.b_type == entity and link.b_id == source_id:
                link.b_id, changed = target_id, True
            if not changed:
                continue
            key = (link.a_type, link.a_id, link.b_type, link.b_id)
            if (link.a_type, link.a_id) == (link.b_type, link.b_id) or key in existing:
                link.soft_delete()
            else:
                existing.add(key)
            result.links += 1
        # 3. local settings
        for key in SETTING_REFS.get(model, ()):
            if get_setting(session, key) == source_id:
                set_setting(session, key, target_id)
        # 4. fill the target's empty fields
        for column in sa_inspect(model).columns:
            name = column.key
            if name in _SKIP:
                continue
            mine, theirs = getattr(target, name), getattr(source, name)
            if (mine is None or mine == "") and theirs not in (None, ""):
                setattr(target, name, theirs)
                result.filled.append(name)
        # 5. retire the source
        note = f"Merged into {target.display_name()} ({target.id[:8]}) on {utcnow():%Y-%m-%d}."
        if hasattr(source, "notes"):
            source.notes = f"{source.notes}\n\n{note}" if source.notes else note
        source.soft_delete()
        result.merged += 1
    session.flush()
    return result


# ---------------------------------------------------------------------------
# Finding duplicates
# ---------------------------------------------------------------------------

def _norm(text: str | None) -> str:
    return " ".join((text or "").lower().split())


def duplicate_key(obj: SyncMixin) -> tuple | None:
    """Records with the same key are probably the same thing. None = never a duplicate."""
    if isinstance(obj, Station):
        place = (obj.locator or "")[:6].lower() or (
            f"{round(obj.latitude, 3)},{round(obj.longitude, 3)}" if obj.latitude is not None else "")
        return ("station", _norm(obj.name), place)
    if isinstance(obj, Equipment):
        return ("equipment", _norm(obj.name), _norm(obj.serial))
    if isinstance(obj, Repeater):
        if obj.output_mhz is None:
            return None
        return ("repeater", round(obj.output_mhz, 3), _norm(obj.callsign) or _norm(obj.name))
    if isinstance(obj, Contact):
        return ("contact", (obj.callsign or "").upper())
    if isinstance(obj, StoredFile):
        return ("file", obj.sha256)
    return None


@dataclass
class DuplicateGroup:
    model: type[SyncMixin]
    records: list  # oldest first: the suggested record to keep

    @property
    def entity(self) -> str:
        return self.model.__entity__


def find_duplicates(session: Session) -> list[DuplicateGroup]:
    groups: list[DuplicateGroup] = []
    for model in MERGEABLE:
        buckets: dict[tuple, list] = {}
        rows = (session.query(model).filter(model.deleted_at.is_(None))
                .order_by(model.created_at).all())
        for row in rows:
            key = duplicate_key(row)
            if key is not None:
                buckets.setdefault(key, []).append(row)
        groups += [DuplicateGroup(model, rows) for rows in buckets.values() if len(rows) > 1]
    return groups


def usage_count(session: Session, obj: SyncMixin) -> int:
    """How many records point at ``obj`` (shown so the busier one can be kept)."""
    total = 0
    for ref_model, column in references_to(type(obj)):
        col = getattr(ref_model, column)
        total += (session.query(ref_model).filter(col == obj.id)
                  .filter(ref_model.deleted_at.is_(None)).count())
    entity = obj.__entity__
    total += session.query(Link).filter(Link.deleted_at.is_(None)).filter(
        ((Link.a_type == entity) & (Link.a_id == obj.id)) |
        ((Link.b_type == entity) & (Link.b_id == obj.id))).count()
    return total
