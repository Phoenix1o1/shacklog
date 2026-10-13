"""End-to-end sync between two real instances (hub + laptop) through the HTTP API.

The laptop's SyncClient talks to the hub app through a transport built on the hub's Flask
test client, so every byte goes through the real endpoints, auth and merge code.
"""

from __future__ import annotations

import io
from datetime import datetime

import pytest

from shacklog.db import set_setting
from shacklog.models import Qso, StoredFile, SyncConflict, SyncPeer
from shacklog.services import sync_engine
from shacklog.services.sync_client import (
    SyncClient,
    SyncError,
    SyncWorker,
    fetch_blob_from_hub,
)
from tests.conftest import build_app

TOKEN = "s3cret-sync-token"


class FlaskTransport:
    """``transport(method, path, json, data, headers) -> (status, body)`` on a test client."""

    def __init__(self, app):
        self.client = app.test_client()
        self.calls: list[tuple[str, str]] = []
        self.fail_on: str | None = None  # path fragment that raises (simulated outage)
        self.fail_after = 0  # let that many matching calls through first

    def __call__(self, method, path, json=None, data=None, headers=None):
        self.calls.append((method, path))
        if self.fail_on and self.fail_on in path:
            if self.fail_after <= 0:
                raise SyncError("network down (simulated)")
            self.fail_after -= 1
        if hasattr(data, "read"):
            data = data.read()
        resp = self.client.open(path, method=method, json=json, data=data, headers=headers)
        body = resp.get_json(silent=True) if resp.is_json else resp.data
        return resp.status_code, body


def _instance(path, mode="desktop"):
    app, db, paths = build_app(path, mode=mode)
    return app, db, paths


@pytest.fixture
def pair(tmp_path):
    hub_app, hub_db, _ = _instance(tmp_path / "hub", mode="server")
    lap_app, lap_db, _ = _instance(tmp_path / "laptop")
    hub_db.rename_instance("Proxmox")
    lap_db.rename_instance("Laptop")
    with hub_db.transaction() as s:
        set_setting(s, "sync_hub_enabled", True)
        set_setting(s, "sync_hub_token", TOKEN)
    with lap_db.transaction() as s:
        set_setting(s, "sync_hub_url", "http://hub.example:8573")
        set_setting(s, "sync_client_token", TOKEN)
    transport = FlaskTransport(hub_app)

    class Pair:
        hub, laptop, t = hub_app, lap_app, transport

        @staticmethod
        def sync(app=lap_app, t=transport):
            client = SyncClient(app.config["DB"], app.config["FILESTORE"],
                                "http://hub.example:8573", TOKEN, transport=t)
            return client.run()

    yield Pair
    hub_db.dispose()
    lap_db.dispose()


def _add_qso(app, call, **kw) -> str:
    with app.config["DB"].transaction() as s:
        q = Qso(call=call, time_on=datetime(2026, 10, 12, 9, 0), band="2m", mode="FM", **kw)
        s.add(q)
        s.flush()
        return q.id


def _get(app, model, rid):
    with app.config["DB"].transaction() as s:
        obj = s.get(model, rid)
        if obj is not None:
            s.expunge(obj)
        return obj


def _count(app, model) -> int:
    with app.config["DB"].transaction() as s:
        return s.query(model).count()


def _edit(app, rid, **values):
    with app.config["DB"].transaction() as s:
        obj = s.get(Qso, rid)
        for k, v in values.items():
            setattr(obj, k, v)


# ---------------------------------------------------------------------------
# API guard rails
# ---------------------------------------------------------------------------

def test_api_requires_enabled_hub_and_valid_token(tmp_path):
    app, db, _ = _instance(tmp_path / "hub", mode="server")
    c = app.test_client()
    body = {"instance_id": "x" * 36, "schema_version": 1}
    r = c.post("/api/sync/hello", json=body, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 403 and "Accept sync" in r.get_json()["error"]
    with db.transaction() as s:
        set_setting(s, "sync_hub_enabled", True)
        set_setting(s, "sync_hub_token", TOKEN)
    assert c.post("/api/sync/hello", json=body).status_code == 401
    r = c.post("/api/sync/hello", json=body, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401 and r.is_json
    r = c.post("/api/sync/hello", json=body, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.get_json()["instance_id"] == db.instance_id
    # Server mode needs a login for pages, but not for the token-authenticated API.
    assert c.get("/qsos/").status_code == 302
    r = c.post("/api/sync/hello", json=dict(body, schema_version=99),
               headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 409 and r.get_json()["update"] == "hub"
    r = c.post("/api/sync/hello", json=dict(body, schema_version=0),
               headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 409 and r.get_json()["update"] == "client"
    with db.transaction() as s:
        peer = s.get(SyncPeer, "x" * 36)
        assert peer.role == "client"
    db.dispose()


def test_blob_endpoints_verify_hashes(pair):
    c = pair.hub.test_client()
    auth = {"Authorization": f"Bearer {TOKEN}"}
    import hashlib
    data = b"manual bytes"
    sha = hashlib.sha256(data).hexdigest()
    r = c.post("/api/sync/blobs/missing", json={"hashes": [sha, "nothex"]}, headers=auth)
    assert r.get_json()["missing"] == [sha]
    assert c.put(f"/api/sync/blob/{'0' * 64}", data=data, headers=auth).status_code == 400
    assert c.put(f"/api/sync/blob/{sha}", data=data, headers=auth).get_json()["stored"] is True
    assert c.get(f"/api/sync/blob/{sha}", headers=auth).data == data
    assert c.get(f"/api/sync/blob/{'1' * 64}", headers=auth).status_code == 404
    # Path tricks never reach the blob store (they don't even match the route).
    assert c.get("/api/sync/blob/..%2F..%2Fsecret.key", headers=auth).status_code != 200


def test_client_reports_bad_token(pair):
    with pytest.raises(SyncError, match="token"):
        SyncClient(pair.laptop.config["DB"], pair.laptop.config["FILESTORE"], "hub", "nope",
                   transport=pair.t).run()


# ---------------------------------------------------------------------------
# Data flows
# ---------------------------------------------------------------------------

def test_records_flow_both_ways_and_rerun_is_noop(pair):
    rid = _add_qso(pair.laptop, "IK2LAP", name="From laptop")
    stats = pair.sync()
    assert stats["pushed"] >= 3  # the QSO and the laptop's two stations
    hub_q = _get(pair.hub, Qso, rid)
    assert hub_q.name == "From laptop" and hub_q.received_from == pair.laptop.config["DB"].instance_id
    # The hub's own stations come back to the laptop; the laptop's QSO doesn't echo back.
    assert stats["inserted"] == 2 and stats["pulled"] == 2

    # Edit on the hub flows back.
    _edit(pair.hub, rid, name="Edited on hub")
    stats = pair.sync()
    assert _get(pair.laptop, Qso, rid).name == "Edited on hub"
    assert stats["pushed"] == 0 and stats["updated"] == 1

    # Nothing left: a re-run changes nothing anywhere.
    counts = (_count(pair.hub, Qso), _count(pair.laptop, Qso))
    seqs = (pair.hub.config["DB"].current_seq(), pair.laptop.config["DB"].current_seq())
    stats = pair.sync()
    assert stats["pushed"] == 0 and stats["pulled"] == 0
    assert (_count(pair.hub, Qso), _count(pair.laptop, Qso)) == counts
    assert (pair.hub.config["DB"].current_seq(), pair.laptop.config["DB"].current_seq()) == seqs
    assert _count(pair.hub, SyncConflict) == _count(pair.laptop, SyncConflict) == 0
    with pair.laptop.config["DB"].transaction() as s:
        hub_peer = s.query(SyncPeer).filter_by(role="hub").one()
        assert hub_peer.name == "Proxmox" and hub_peer.last_status == "ok"


def test_soft_delete_propagates(pair):
    rid = _add_qso(pair.laptop, "IK2DEL")
    pair.sync()
    with pair.laptop.config["DB"].transaction() as s:
        s.get(Qso, rid).soft_delete()
    pair.sync()
    assert _get(pair.hub, Qso, rid).deleted_at is not None
    # And a restore on the hub undeletes it on the laptop.
    with pair.hub.config["DB"].transaction() as s:
        s.get(Qso, rid).restore()
    pair.sync()
    assert _get(pair.laptop, Qso, rid).deleted_at is None


def test_concurrent_edits_make_exactly_one_conflict_and_newer_wins(pair):
    rid = _add_qso(pair.laptop, "IK2CON", name="Original")
    pair.sync()
    _edit(pair.laptop, rid, name="Laptop edit")      # older
    _edit(pair.hub, rid, name="Hub edit (newer)")    # newer
    stats = pair.sync()
    assert stats["hub_conflicts"] == 1
    assert _get(pair.hub, Qso, rid).name == "Hub edit (newer)"
    assert _get(pair.laptop, Qso, rid).name == "Hub edit (newer)"
    total = _count(pair.hub, SyncConflict) + _count(pair.laptop, SyncConflict)
    assert total == 1
    with pair.hub.config["DB"].transaction() as s:
        c = s.query(SyncConflict).one()
        assert c.kind == "incoming_discarded" and c.peer_name == "Laptop"
        assert '"Laptop edit"' in c.loser_snapshot
        cid = c.id
    pair.sync()  # no new conflicts on re-run
    assert _count(pair.hub, SyncConflict) + _count(pair.laptop, SyncConflict) == 1

    # Restoring the loser on the hub is a normal edit: it reaches the laptop.
    with pair.hub.config["DB"].transaction() as s:
        sync_engine.restore_conflict(s, cid)
    pair.sync()
    assert _get(pair.laptop, Qso, rid).name == "Laptop edit"
    assert _get(pair.hub, Qso, rid).name == "Laptop edit"


def test_newer_laptop_edit_wins_on_hub_and_hub_version_is_kept(pair):
    rid = _add_qso(pair.laptop, "IK2NEW", name="Original")
    pair.sync()
    _edit(pair.hub, rid, name="Hub edit (older)")
    _edit(pair.laptop, rid, name="Laptop edit (newer)")
    pair.sync()
    assert _get(pair.hub, Qso, rid).name == "Laptop edit (newer)"
    assert _get(pair.laptop, Qso, rid).name == "Laptop edit (newer)"
    with pair.hub.config["DB"].transaction() as s:
        c = s.query(SyncConflict).one()
        assert c.kind == "local_overwritten" and '"Hub edit (older)"' in c.loser_snapshot
    assert _count(pair.laptop, SyncConflict) == 0


def test_file_blobs_travel_both_ways(pair):
    lap_store, hub_store = pair.laptop.config["FILESTORE"], pair.hub.config["FILESTORE"]
    sha_up, size = lap_store.put_bytes(b"%PDF-1.4 TM-733E manual")
    with pair.laptop.config["DB"].transaction() as s:
        s.add(StoredFile(sha256=sha_up, filename="tm733.pdf", size=size, category="manual"))
    sha_down, size2 = hub_store.put_bytes(b"UV-K5 CHIRP image bytes")
    with pair.hub.config["DB"].transaction() as s:
        s.add(StoredFile(sha256=sha_down, filename="uvk5.img", size=size2, category="chirp"))
    stats = pair.sync()
    assert stats["blobs_up"] == 1 and stats["blobs_down"] == 1
    assert hub_store.read_bytes(sha_up) == b"%PDF-1.4 TM-733E manual"
    assert lap_store.read_bytes(sha_down) == b"UV-K5 CHIRP image bytes"
    stats = pair.sync()
    assert stats["blobs_up"] == stats["blobs_down"] == 0


def test_blob_on_demand_when_pull_files_is_off(pair):
    hub_store, lap_store = pair.hub.config["FILESTORE"], pair.laptop.config["FILESTORE"]
    sha, size = hub_store.put_bytes(b"big recording")
    with pair.hub.config["DB"].transaction() as s:
        s.add(StoredFile(sha256=sha, filename="rec.wav", size=size))
    with pair.laptop.config["DB"].transaction() as s:
        set_setting(s, "sync_pull_files", False)
    pair.sync()
    assert not lap_store.exists(sha)
    assert fetch_blob_from_hub(pair.laptop.config["DB"], lap_store, sha, transport=pair.t)
    assert lap_store.read_bytes(sha) == b"big recording"


def test_interrupted_push_resumes_without_duplicates(pair):
    ids = [_add_qso(pair.laptop, f"IK2{i:03d}") for i in range(7)]
    t = pair.t
    t.fail_on, t.fail_after = "/push", 1  # first batch gets through, the second fails

    class Small(SyncClient):
        push_batch = 3

    def run_small():
        return Small(pair.laptop.config["DB"], pair.laptop.config["FILESTORE"], "hub", TOKEN,
                     transport=t).run()

    with pytest.raises(SyncError, match="network down"):
        run_small()
    with pair.laptop.config["DB"].transaction() as s:
        peer = s.query(SyncPeer).filter_by(role="hub").one()
        assert peer.last_push_seq > 0 and peer.last_status == "error"
    on_hub = _count(pair.hub, Qso)
    assert 0 < on_hub < 7
    t.fail_on = None
    stats = run_small()
    assert _count(pair.hub, Qso) == 7
    assert stats["pushed"] < 9  # only the rest was sent (7 QSOs + 2 stations in total)
    assert all(_get(pair.hub, Qso, rid) is not None for rid in ids)
    assert _count(pair.hub, SyncConflict) == 0


def test_interrupted_pull_resumes(pair):
    for i in range(5):
        _add_qso(pair.hub, f"F1{i:03d}")
    t = pair.t
    t.fail_on, t.fail_after = "/pull", 1

    class Small(SyncClient):
        pull_batch = 3  # first page: the hub's two stations and one QSO

    client = Small(pair.laptop.config["DB"], pair.laptop.config["FILESTORE"], "hub", TOKEN,
                   transport=t)
    with pytest.raises(SyncError):
        client.run()
    assert 0 < _count(pair.laptop, Qso) < 5
    t.fail_on = None
    Small(pair.laptop.config["DB"], pair.laptop.config["FILESTORE"], "hub", TOKEN,
          transport=t).run()
    assert _count(pair.laptop, Qso) == 5


def test_three_instances_converge_through_the_hub(pair, tmp_path):
    desk_app, desk_db, _ = _instance(tmp_path / "desktop")
    try:
        rid = _add_qso(pair.laptop, "IK2THR")
        pair.sync()
        pair.sync(app=desk_app)
        assert _get(desk_app, Qso, rid) is not None
        _edit(desk_app, rid, name="Desk")
        pair.sync(app=desk_app)
        pair.sync()
        assert _get(pair.laptop, Qso, rid).name == "Desk"
        # Six stations everywhere (two per instance), one QSO, no conflicts.
        from shacklog.models import Station
        assert _count(pair.laptop, Station) == _count(pair.hub, Station) == 6
        assert _count(pair.hub, SyncConflict) == 0
    finally:
        desk_db.dispose()


def test_restored_laptop_gets_its_records_back(pair, tmp_path):
    from shacklog.services import backup
    db = pair.laptop.config["DB"]
    old = backup.create_backup(db, tmp_path / "bk", "manual")
    rid = _add_qso(pair.laptop, "IK2LOST", name="Only on hub after restore")
    pair.sync()
    # Disaster: the laptop goes back to the old backup.
    db.dispose()
    import shutil
    for suffix in ("-wal", "-shm"):
        (db.path.with_name(db.path.name + suffix)).unlink(missing_ok=True)
    shutil.copyfile(old, db.path)
    assert _get(pair.laptop, Qso, rid) is None
    pair.sync()
    assert _get(pair.laptop, Qso, rid).name == "Only on hub after restore"
    _add_qso(pair.laptop, "IK2AFTER")
    pair.sync()
    assert _count(pair.hub, Qso) == 2 and _count(pair.hub, SyncConflict) == 0


def test_restored_hub_gets_records_back_from_laptop(pair, tmp_path):
    from shacklog.services import backup
    hub_db = pair.hub.config["DB"]
    old = backup.create_backup(hub_db, tmp_path / "bk", "manual")
    rid = _add_qso(pair.laptop, "IK2KEEP")
    hub_rid = _add_qso(pair.hub, "F1HUB")
    pair.sync()
    hub_db.dispose()
    import shutil
    for suffix in ("-wal", "-shm"):
        (hub_db.path.with_name(hub_db.path.name + suffix)).unlink(missing_ok=True)
    shutil.copyfile(old, hub_db.path)
    assert _get(pair.hub, Qso, rid) is None
    pair.sync()
    assert _get(pair.hub, Qso, rid) is not None
    assert _get(pair.hub, Qso, hub_rid) is not None  # its own lost record, from the laptop


def test_worker_runs_on_trigger_and_reports_status(pair):
    worker = SyncWorker(pair.laptop, transport=pair.t)
    worker.first_delay_s = 3600  # only the trigger may start it
    with pair.laptop.config["DB"].transaction() as s:
        set_setting(s, "sync_interval_min", 0)
    worker.start()
    try:
        assert pair.laptop.config["SYNC_WORKER"] is worker
        rid = _add_qso(pair.laptop, "IK2WRK")
        worker.trigger()
        import time
        for _ in range(100):
            if pair.laptop.config["SYNC_STATUS"].get("state") == "ok":
                break
            time.sleep(0.05)
        status = pair.laptop.config["SYNC_STATUS"]
        assert status["state"] == "ok" and status["configured"] and status["last_run"]
        assert _get(pair.hub, Qso, rid) is not None
    finally:
        worker.stop()


def test_worker_survives_errors(pair):
    pair.t.fail_on = "/hello"
    worker = SyncWorker(pair.laptop, transport=pair.t)
    worker.first_delay_s = 3600
    worker.start()
    try:
        worker.trigger()
        import time
        for _ in range(100):
            if pair.laptop.config["SYNC_STATUS"].get("state") == "error":
                break
            time.sleep(0.05)
        status = pair.laptop.config["SYNC_STATUS"]
        assert status["state"] == "error" and "network down" in status["last_error"]
        assert worker._thread.is_alive()
    finally:
        worker.stop()


def test_merge_follows_late_references_on_every_instance(pair):
    """A QSO logged on the hub after the laptop's last pull still ends up on the kept copy."""
    from shacklog.models import Station
    from shacklog.services import merge

    pair.sync()
    with pair.hub.config["DB"].transaction() as s:
        hub_home = s.query(Station).filter_by(origin_instance=pair.hub.config["DB"].instance_id,
                                              name="Milano").one().id
    with pair.laptop.config["DB"].transaction() as s:
        lap_home = s.query(Station).filter_by(origin_instance=pair.laptop.config["DB"].instance_id,
                                              name="Milano").one().id
    late = _add_qso(pair.hub, "IK2LATE", station_id=hub_home)  # logged on the phone
    with pair.laptop.config["DB"].transaction() as s:
        merge.merge(s, Station, lap_home, [hub_home])
    pair.sync()
    pair.sync()  # the hub's re-pointing (made during the push) comes back on this one
    for app in (pair.hub, pair.laptop):
        assert _get(app, Qso, late).station_id == lap_home
        assert _get(app, Station, hub_home).deleted_at is not None
