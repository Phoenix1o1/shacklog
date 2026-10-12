"""Settings pages, first-run setup (new and join-the-hub), login in server mode."""

from shacklog.db import get_setting, set_setting
from shacklog.models import Qso, Station
from shacklog.services import sync_client
from tests.conftest import build_app, csrf, extract_csrf
from tests.test_sync_api import TOKEN, FlaskTransport


def test_every_settings_page_renders(client):
    for url in ("/settings/", "/settings/sync", "/settings/data", "/settings/conflicts",
                "/settings/trash"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert "<h1>" in r.text


def test_trash_lists_and_restores(client, s):
    station = s.query(Station).filter_by(name="Grenoble").one()
    assert client.post(f"/stations/{station.id}/delete",
                       data={"_csrf": csrf(client)}).status_code == 302
    page = client.get("/settings/trash").text
    assert "Grenoble" in page
    r = client.post(f"/settings/trash/station/{station.id}/restore", data={"_csrf": csrf(client)})
    assert r.status_code == 302
    s.expire_all()
    assert s.get(Station, station.id).deleted_at is None


def test_new_instance_setup_creates_stations(tmp_path):
    app, db, _ = build_app(tmp_path / "fresh", setup=False)
    c = app.test_client()
    assert c.get("/").headers["Location"].endswith("/setup")
    token = extract_csrf(c.get("/setup").text)
    r = c.post("/setup", data={"_csrf": token, "setup_kind": "new", "instance_name": "Desk",
                               "callsign": "iu2tst", "home_name": "Home",
                               "home_position": "45.464, 9.19"})
    assert r.status_code == 302
    with db.transaction() as s:
        home = s.query(Station).one()
        assert home.locator == "JN45ol" and home.callsign == "IU2TST"
        assert get_setting(s, "default_station_id") == home.id
    assert db.instance_name == "Desk"
    db.dispose()


def test_join_hub_at_setup_pulls_instead_of_creating(tmp_path, monkeypatch):
    hub_app, hub_db, _ = build_app(tmp_path / "hub", mode="server")
    with hub_db.transaction() as s:
        set_setting(s, "sync_hub_enabled", True)
        set_setting(s, "sync_hub_token", TOKEN)
        station = s.query(Station).filter_by(name="Milano").one()
        s.add(Qso(call="IK2ABC", time_on=station.created_at, station_id=station.id))

    # The new instance talks to the hub through its test client instead of the network.
    transport = FlaskTransport(hub_app)
    original = sync_client.SyncClient.from_settings.__func__
    monkeypatch.setattr(sync_client.SyncClient, "from_settings", classmethod(
        lambda cls, db, fs, t=None: original(cls, db, fs, transport)))

    app, db, _ = build_app(tmp_path / "laptop", setup=False)
    c = app.test_client()
    token = extract_csrf(c.get("/setup").text)
    r = c.post("/setup", data={"_csrf": token, "setup_kind": "join", "instance_name": "Laptop",
                               "hub_url": "http://hub.example:8573", "hub_token": TOKEN})
    assert r.status_code == 302, r.text
    with db.transaction() as s:
        names = sorted(st.name for st in s.query(Station).all())
        assert names == ["Grenoble", "Milano"]  # the hub's, not new duplicates
        assert s.query(Qso).filter_by(call="IK2ABC").count() == 1
        assert get_setting(s, "setup_done") is True
    hub_db.dispose()
    db.dispose()


def test_server_mode_requires_login_and_setup_code(tmp_path):
    app, db, _ = build_app(tmp_path / "srv", mode="server")
    app.config["SETUP_CODE"] = "1234-5678"
    c = app.test_client()
    assert "/login" in c.get("/qsos/").headers["Location"]
    token = extract_csrf(c.get("/login").text)
    bad = c.post("/login", data={"_csrf": token, "setup_code": "0000-0000",
                                 "password": "longenough", "password2": "longenough"})
    assert "doesn" in bad.text  # "doesn't match"
    ok = c.post("/login", data={"_csrf": token, "setup_code": "1234-5678",
                                "password": "longenough", "password2": "longenough"})
    assert ok.status_code == 302
    assert c.get("/qsos/").status_code == 200
    db.dispose()
