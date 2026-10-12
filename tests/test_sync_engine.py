"""sync_engine: serialisation, change paging and the merge rule of PLAN.md §6."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from shacklog.models import (
    Equipment,
    Qso,
    RadioConfig,
    Station,
    StoredFile,
    SyncConflict,
    new_uuid,
    utcnow,
)
from shacklog.services import sync_engine as se

PEER = "aaaaaaaa-0000-4000-8000-000000000001"
OTHER = "bbbbbbbb-0000-4000-8000-000000000002"


def _qso(s, call="IK2ABC", **kw) -> Qso:
    q = Qso(call=call, time_on=datetime(2026, 10, 12, 10, 5), band="2m", mode="FM", **kw)
    s.add(q)
    s.commit()
    return q


def _incoming(obj_row: dict, **changes) -> dict:
    row = dict(obj_row)
    row.update(changes)
    return row


def test_serialize_is_json_safe_and_skips_local_columns(s):
    q = _qso(s, freq_mhz=145.5)
    cfg = RadioConfig(title="UV-K5", config_date=date(2026, 10, 1), is_current=True)
    s.add(cfg)
    s.commit()
    row = se.serialize(q)
    assert "change_seq" not in row and "received_from" not in row
    assert row["time_on"] == "2026-10-12T10:05:00"
    assert row["freq_mhz"] == 145.5 and row["name"] is None
    json.dumps(row)  # must not raise
    crow = se.serialize(cfg)
    assert crow["config_date"] == "2026-10-01" and crow["is_current"] is True
    # Round trip back to Python types.
    parsed = se._parse_row(RadioConfig, crow)
    assert parsed["config_date"] == date(2026, 10, 1)
    assert isinstance(parsed["updated_at"], datetime)


def test_changes_are_ordered_paged_and_excluded_peer_still_advances(s, db):
    start = db.current_seq()
    a = _qso(s, "IK2AAA")
    e = Equipment(name="TM-733E", kind="radio")
    s.add(e)
    s.commit()
    b = _qso(s, "IK2BBB")
    # A record that came from PEER: excluded when talking to PEER.
    se.apply_records(s, [{"table": "qsos", "row": _incoming(se.serialize(b), id=new_uuid(),
                                                           call="F1PEER")}],
                     PEER, "peer", mode="import")
    s.commit()

    records, last = se.changes_since(s, start, None, 2)
    assert [r["row"]["id"] for r in records] == [a.id, e.id]
    assert last == e.change_seq
    records2, last2 = se.changes_since(s, last, None, 100)
    assert [r["table"] for r in records2] == ["qsos", "qsos"]
    assert last2 == db.current_seq()

    records3, last3 = se.changes_since(s, start, PEER, 100)
    calls = [r["row"].get("call") for r in records3]
    assert "F1PEER" not in calls and len(records3) == 3
    assert last3 == db.current_seq()  # watermark moved past the excluded row
    assert se.changes_since(s, last3, PEER, 100) == ([], last3)


def test_apply_inserts_keeps_stamps_and_marks_origin(s, db):
    when = datetime(2026, 1, 2, 3, 4, 5, 123456)
    rid = new_uuid()
    rec = {"table": "equipment", "row": {
        "id": rid, "name": "NanoVNA", "kind": "meter", "created_at": when.isoformat(),
        "updated_at": when.isoformat(), "modified_by_instance": PEER, "origin_instance": PEER,
        "deleted_at": None, "acquired_on": "2025-12-24", "brand_new_column": "kept out"}}
    stats = se.apply_records(s, [rec, {"table": "future_table", "row": {"id": "x"}}],
                             PEER, "Laptop", mode="import")
    s.commit()
    assert stats["inserted"] == 1 and stats["ignored"] == 1
    e = s.get(Equipment, rid)
    assert e.updated_at == when and e.modified_by_instance == PEER
    assert e.received_from == PEER and e.acquired_on == date(2025, 12, 24)
    assert e.change_seq == db.current_seq()
    # Same version again: nothing happens.
    assert se.apply_records(s, [rec], PEER, "Laptop", mode="import")["unchanged"] == 1


def test_import_newer_wins_and_loser_is_kept(s):
    q = _qso(s, name="Marco")
    older = _incoming(se.serialize(q), name="Old name",
                      updated_at=(q.updated_at - timedelta(days=1)).isoformat(),
                      modified_by_instance=PEER)
    stats = se.apply_records(s, [{"table": "qsos", "row": older}], PEER, "Laptop", mode="import")
    s.commit()
    assert stats["conflicts"] == 1 and s.get(Qso, q.id).name == "Marco"
    c = s.query(SyncConflict).one()
    assert c.kind == "incoming_discarded" and json.loads(c.loser_snapshot)["name"] == "Old name"
    assert json.loads(c.winner_snapshot)["name"] == "Marco"

    newer = _incoming(se.serialize(q), name="New name",
                      updated_at=(q.updated_at + timedelta(days=1)).isoformat(),
                      modified_by_instance=PEER)
    stats = se.apply_records(s, [{"table": "qsos", "row": newer}], PEER, "Laptop", mode="import")
    s.commit()
    assert stats["updated"] == 1 and s.get(Qso, q.id).name == "New name"
    lost = s.query(SyncConflict).filter_by(kind="local_overwritten").one()
    assert json.loads(lost.loser_snapshot)["name"] == "Marco"
    # Importing both again changes nothing and stores no duplicate conflicts.
    for row in (older, newer):
        se.apply_records(s, [{"table": "qsos", "row": row}], PEER, "Laptop", mode="import")
    s.commit()
    assert s.query(SyncConflict).count() == 2


def test_same_data_different_stamps_is_not_a_conflict(s):
    q = _qso(s)
    newer = _incoming(se.serialize(q), updated_at=(q.updated_at + timedelta(seconds=5)).isoformat(),
                      modified_by_instance=PEER)
    stats = se.apply_records(s, [{"table": "qsos", "row": newer}], PEER, None, mode="import")
    s.commit()
    assert stats["conflicts"] == 0 and s.query(SyncConflict).count() == 0
    assert s.get(Qso, q.id).modified_by_instance == PEER


def test_hub_mode_only_conflicts_when_client_had_not_seen_our_version(s, db):
    q = _qso(s, name="Hub edit")
    client_row = _incoming(se.serialize(q), name="Client edit",
                           updated_at=(q.updated_at + timedelta(seconds=1)).isoformat(),
                           modified_by_instance=PEER)
    # The client pulled past our version: its edit simply applies.
    stats = se.apply_records(s, [{"table": "qsos", "row": client_row}], PEER, "Laptop",
                             mode="hub", client_last_pull=q.change_seq)
    s.commit()
    assert stats["conflicts"] == 0 and s.get(Qso, q.id).name == "Client edit"

    # Now the hub edits again; a client that hasn't pulled it sends an *older* edit.
    q = s.get(Qso, q.id)
    q.name = "Hub edit 2"
    s.commit()
    stale = _incoming(client_row, name="Client edit 2",
                      updated_at=(q.updated_at - timedelta(seconds=1)).isoformat())
    stats = se.apply_records(s, [{"table": "qsos", "row": stale}], PEER, "Laptop",
                             mode="hub", client_last_pull=q.change_seq - 1)
    s.commit()
    assert stats["conflicts"] == 1 and s.get(Qso, q.id).name == "Hub edit 2"
    assert s.query(SyncConflict).one().kind == "incoming_discarded"


def test_client_mode_hub_wins_unless_local_edit_after_push(s):
    q = _qso(s, name="Local")
    hub_row = _incoming(se.serialize(q), name="From hub",
                        updated_at=(q.updated_at - timedelta(seconds=1)).isoformat(),
                        modified_by_instance=PEER)
    # Local edit already pushed (change_seq <= last push): the hub's version applies even
    # though its stamp is older — the hub already resolved it.
    stats = se.apply_records(s, [{"table": "qsos", "row": hub_row}], PEER, "Hub",
                             mode="client", local_dirty_after=q.change_seq)
    s.commit()
    assert stats["updated"] == 1 and s.get(Qso, q.id).name == "From hub"
    assert s.query(SyncConflict).count() == 0

    q = s.get(Qso, q.id)
    q.name = "Edited during sync"
    s.commit()
    hub_row2 = _incoming(hub_row, name="Hub again",
                         updated_at=(q.updated_at + timedelta(seconds=1)).isoformat())
    stats = se.apply_records(s, [{"table": "qsos", "row": hub_row2}], PEER, "Hub",
                             mode="client", local_dirty_after=q.change_seq - 1)
    s.commit()
    assert stats["conflicts"] == 1 and s.get(Qso, q.id).name == "Hub again"
    c = s.query(SyncConflict).one()
    assert c.kind == "local_overwritten" and c.peer_name == "Hub"


def test_restore_and_dismiss_conflict(s, db):
    q = _qso(s, name="Mine")
    newer = _incoming(se.serialize(q), name="Theirs",
                      updated_at=(q.updated_at + timedelta(minutes=1)).isoformat(),
                      modified_by_instance=PEER)
    se.apply_records(s, [{"table": "qsos", "row": newer}], PEER, "Laptop", mode="import")
    s.commit()
    c = s.query(SyncConflict).one()
    view = se.describe_conflict(s, c)
    assert ("name", "Mine", "Theirs") in view.rows
    before = db.current_seq()
    se.restore_conflict(s, c.id)
    s.commit()
    q = s.get(Qso, q.id)
    assert q.name == "Mine" and q.received_from is None
    assert q.modified_by_instance == db.instance_id and q.change_seq > before
    assert c.resolution == "restored" and c.resolved_at is not None

    other = _incoming(se.serialize(q), name="Third",
                      updated_at=(q.updated_at + timedelta(minutes=1)).isoformat(),
                      modified_by_instance=OTHER)
    se.apply_records(s, [{"table": "qsos", "row": other}], OTHER, "Desktop", mode="import")
    s.commit()
    c2 = s.query(SyncConflict).filter(SyncConflict.resolved_at.is_(None)).one()
    se.dismiss_conflict(s, c2.id)
    s.commit()
    assert c2.resolution == "dismissed" and s.get(Qso, q.id).name == "Third"
    with pytest.raises(LookupError):
        se.restore_conflict(s, 9999)


def test_restore_recreates_a_missing_record(s):
    rid = new_uuid()
    snap = {"id": rid, "name": "Lost station", "kind": "portable", "created_at": utcnow().isoformat()}
    c = SyncConflict(table_name="stations", record_id=rid, kind="incoming_discarded",
                     loser_snapshot=json.dumps(snap))
    s.add(c)
    s.commit()
    se.restore_conflict(s, c.id)
    s.commit()
    assert s.get(Station, rid).name == "Lost station"


def test_unreadable_value_is_kept_not_fatal(s):
    q = _qso(s)
    bad = _incoming(se.serialize(q), time_on="not a date", updated_at="2030-01-01T00:00:00",
                    modified_by_instance=PEER)
    good = {"table": "equipment", "row": {"id": new_uuid(), "name": "Coax", "kind": "feedline",
                                          "updated_at": "2026-01-01T00:00:00"}}
    stats = se.apply_records(s, [{"table": "qsos", "row": bad}, good], PEER, None, mode="import")
    s.commit()
    assert stats["errors"] == 1 and stats["inserted"] == 1
    assert s.query(SyncConflict).one().table_name == "qsos"


def test_null_in_required_column_does_not_abort_batch(s):
    rid = new_uuid()
    rec = {"table": "equipment", "row": {"id": rid, "name": "Dummy load", "kind": None,
                                         "status": None, "updated_at": "2026-01-01T00:00:00"}}
    se.apply_records(s, [rec], PEER, None, mode="import")
    s.commit()
    e = s.get(Equipment, rid)
    assert e.kind == "radio" and e.status == "active"


def test_page_size_cap_lets_one_big_record_through(s, db):
    start = db.current_seq()
    for i in range(3):
        sf = StoredFile(sha256=f"{i:064x}", filename=f"m{i}.txt", text_content="x" * 5000)
        s.add(sf)
    s.commit()
    page = se.changes_page(s, start, None, limit=100, max_bytes=1000)
    assert len(page.records) == 1 and page.more
    assert page.records[0]["row"]["text_content"] == "x" * 5000
