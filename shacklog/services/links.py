"""Generic links between records (see :class:`shacklog.models.Link`).

A link connects any two synced records, e.g. a radio and its manual, a test entry and its
NanoVNA sweep, a QSO and its QSL card photo, a repeater and the CHIRP config that holds it.
Pages call :func:`links_for` to show everything connected to a record, in both directions.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from shacklog.models import ENTITIES, Link, SyncMixin


@dataclass
class LinkedItem:
    link: Link
    entity: str  # entity name of the *other* record
    obj: SyncMixin | None  # the other record (None if it isn't on this instance yet)
    role: str | None


def get_entity(session: Session, entity: str, entity_id: str) -> SyncMixin | None:
    model = ENTITIES.get(entity)
    if model is None:
        return None
    return session.get(model, entity_id)


def links_for(session: Session, entity: str, entity_id: str) -> list[LinkedItem]:
    """Every live link touching (entity, id), resolved to the record on the other end."""
    rows = (
        session.query(Link)
        .filter(Link.deleted_at.is_(None))
        .filter(or_(and_(Link.a_type == entity, Link.a_id == entity_id),
                    and_(Link.b_type == entity, Link.b_id == entity_id)))
        .order_by(Link.created_at)
        .all()
    )
    items: list[LinkedItem] = []
    for link in rows:
        if link.a_type == entity and link.a_id == entity_id:
            other_type, other_id = link.b_type, link.b_id
        else:
            other_type, other_id = link.a_type, link.a_id
        other = get_entity(session, other_type, other_id)
        if other is not None and other.deleted_at is not None:
            continue  # the other side is in the trash
        items.append(LinkedItem(link, other_type, other, link.role))
    return items


def add_link(session: Session, a: SyncMixin, b: SyncMixin, role: str | None = None,
             note: str | None = None) -> Link:
    """Link two records, reusing (or undeleting) an existing identical link."""
    existing = (
        session.query(Link)
        .filter(or_(
            and_(Link.a_type == a.__entity__, Link.a_id == a.id,
                 Link.b_type == b.__entity__, Link.b_id == b.id),
            and_(Link.a_type == b.__entity__, Link.a_id == b.id,
                 Link.b_type == a.__entity__, Link.b_id == a.id),
        ))
        .first()
    )
    if existing is not None:
        existing.deleted_at = None
        if role:
            existing.role = role
        if note:
            existing.note = note
        return existing
    link = Link(a_type=a.__entity__, a_id=a.id, b_type=b.__entity__, b_id=b.id,
                role=role, note=note)
    session.add(link)
    return link


def linked_of_type(session: Session, obj: SyncMixin, entity: str, role: str | None = None) -> list:
    """Shortcut: the records of one type linked to ``obj`` (optionally with a role)."""
    return [i.obj for i in links_for(session, obj.__entity__, obj.id)
            if i.entity == entity and i.obj is not None and (role is None or i.role == role)]
