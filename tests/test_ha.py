"""Home Assistant: read-only token, /api/ha/state, the YAML for configuration.yaml."""

import re
from datetime import datetime, timedelta

import pytest

from shacklog.db import get_setting
from shacklog.models import JournalEntry, Qso, SwlEntry, SyncPeer, utcnow
from shacklog.services import backup, ha
from tests.conftest import build_app, csrf


@pytest.fixture
def hub(tmp_path):
    app, db, paths = build_app(tmp_path / "hub", mode="server")
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["logged_in"] = True
    yield app, db, paths, c
    db.dispose()


def _make_token(c) -> str:
    page = c.post("/settings/", data={"_csrf": csrf(c), "action": "ha_token"}).text
    return re.search(r"(shk_ha_[A-Za-z0-9_-]+)", page).group(1)


def test_token_lifecycle(hub):
    app, db, _paths, c = hub
    api = app.test_client()  # Home Assistant: no login, no cookies
    assert api.get("/api/ha/state").status_code == 403  # no token yet
    token = _make_token(c)
    with db.transaction() as s:
        stored = get_setting(s, "ha_token_hash")
    assert stored == ha.token_hash(token) and token not in stored  # only the hash is kept
    assert token not in c.get("/settings/").text  # shown once
    assert api.get("/api/ha/state").status_code == 401
    assert api.get("/api/ha/state", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = api.get("/api/ha/state", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json["instance"]["role"] == "hub"
    # the token reads this summary only: not the web pages, not the sync API
    assert api.get("/qsos/", headers={"Authorization": f"Bearer {token}"}).status_code == 302
    assert api.post("/api/sync/hello", json={}, headers={"Authorization": f"Bearer {token}"}).status_code in (401, 403)
    new = _make_token(c)  # a new token replaces the old one
    assert api.get("/api/ha/state", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    c.post("/settings/", data={"_csrf": csrf(c), "action": "ha_off"})
    assert api.get("/api/ha/state", headers={"Authorization": f"Bearer {new}"}).status_code == 403


def test_state_document(hub):
    app, db, paths, c = hub
    now = utcnow()
    with db.transaction() as s:
        s.add(Qso(call="IK2ABC", time_on=now - timedelta(minutes=10), band="2m", mode="FM",
                  freq_mhz=145.5, country="Italy"))
        s.add(Qso(call="F4XYZ", time_on=now - timedelta(days=3), band="70cm", mode="FM",
                  country="France"))
        s.add(SwlEntry(time_utc=now - timedelta(hours=2), heard="IR2UFD", freq_mhz=145.6125))
        s.add(JournalEntry(kind="diary", time_utc=now - timedelta(days=1), title="New antenna"))
        s.add(SyncPeer(instance_id="a" * 36, name="Laptop", role="client",
                       last_sync_at=now - timedelta(hours=30), last_status="ok"))
        s.add(SyncPeer(instance_id="b" * 36, name="Shack PC", role="client",
                       last_sync_at=now - timedelta(minutes=12), last_status="ok"))
    backup.create_backup(db, paths.backups, reason="manual")
    token = _make_token(c)
    doc = app.test_client().get("/api/ha/state", headers={"Authorization": f"Bearer {token}"}).json
    assert doc["active"] is True
    assert doc["qsos"]["total"] == 2 and doc["qsos"]["last"]["call"] == "IK2ABC"
    assert doc["qsos"]["today"] in (1, 2)  # 2 only within 10 minutes after midnight
    assert doc["qsos"]["last"]["time_utc"].endswith("+00:00")
    assert doc["swl"]["last"]["heard"] == "IR2UFD" and doc["diary"]["last"]["title"] == "New antenna"
    assert doc["countries"] == 2
    assert doc["backup"]["last"].endswith("+00:00") and doc["backup"]["hours_ago"] < 1
    sync = doc["sync"]
    assert [p["name"] for p in sync["peers"]] == ["Shack PC", "Laptop"]
    assert sync["oldest_hours"] == pytest.approx(30, abs=0.2)
    assert sync["last"].endswith("+00:00")
    assert doc["station"]["name"] == "Milano" and doc["update_available"] is False


def test_pc_state_and_quiet_station(tmp_path):
    app, db, _paths = build_app(tmp_path / "pc")
    with app.test_request_context():
        with db.transaction() as s:
            doc = ha.state(s, now=datetime(2026, 10, 14, 12), instance={"role": "pc"},
                           sync_status={"configured": True, "state": "error",
                                        "last_error": "hub unreachable"})
    assert doc["active"] is False and doc["qsos"]["last"] is None
    assert doc["sync"] == {"role": "pc", "last": None, "peers": [], "oldest_hours": None,
                           "state": "error", "error": "hub unreachable"}
    db.dispose()


def test_yaml_is_valid_and_uses_real_fields(hub):
    yaml = pytest.importorskip("yaml")
    app, db, paths, c = hub
    with db.transaction() as s:  # something in every section, so every field is filled
        s.add(Qso(call="IK2ABC", time_on=utcnow(), band="2m", mode="FM"))
        s.add(SwlEntry(time_utc=utcnow(), heard="IR2UFD"))
    token = _make_token(c)
    page = c.get("/settings/").text
    assert "/api/ha/state" in page and "rest:" in page
    text = ha.yaml_config("http://192.168.1.50:8573/")

    class Loader(yaml.SafeLoader):
        pass
    Loader.add_constructor("!secret", lambda loader, node: f"<secret {node.value}>")
    config = yaml.load(text, Loader=Loader)
    rest = config["rest"][0]
    assert rest["resource"] == "http://192.168.1.50:8573/api/ha/state"
    assert rest["headers"]["Authorization"] == "<secret shacklog_token>"
    doc = app.test_client().get("/api/ha/state", headers={"Authorization": f"Bearer {token}"}).json
    for sensor in rest["sensor"] + rest["binary_sensor"]:
        for path in re.findall(r"value_json\.([a-z_.]+)", sensor["value_template"]):
            node = doc
            for part in path.split("."):
                assert part in node, f"{sensor['name']}: {path}"
                node = node[part] if node[part] is not None else {}
    assert len({s["unique_id"] for s in rest["sensor"] + rest["binary_sensor"]}) == 10
