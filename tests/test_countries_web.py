"""Country list in the web app: filled in on new QSOs, call hint, callsign page,
Settings › Data (upload, fill in, monthly switch) and statistics."""

import io

from shacklog.models import Qso
from shacklog.services import cty
from tests.conftest import csrf
from tests.test_cty import fake_list
from tests.test_qsos_web import _post_qso


def _install(app):
    cty.install(app.config["PATHS"].data_dir, fake_list())


def test_without_a_list_nothing_changes(client, s):
    _post_qso(client, s)
    assert s.query(Qso).one().country is None
    page = client.get("/settings/data").text
    assert "No country list" in page and "Download the country list" in page


def test_new_qso_gets_its_country_unless_typed(app, client, s):
    _install(app)
    _post_qso(client, s)
    _post_qso(client, s, call="IZ1XYZ", country="Vatican?", time_on="2026-10-12T11:00")
    countries = {q.call: q.country for q in s.query(Qso)}
    assert countries == {"IK2ABC": "Italy", "IZ1XYZ": "Vatican?"}


def test_call_hint_and_callsign_page(app, client, s):
    _install(app)
    hint = client.get("/qsos/check-call?call=ik2abc").text
    assert "New country." in hint and "Italy · EU · CQ 15 · ITU 28 · DXCC 248" in hint
    assert "beam" in hint  # from the home station of the test setup
    _post_qso(client, s)
    assert "New country." not in client.get("/qsos/check-call?call=iz5zzz").text
    assert "DXCC 248" in client.get("/contacts/call/IK2ABC").text


def test_upload_fill_and_switch(app, client, s):
    _post_qso(client, s)  # logged before there was a list
    _post_qso(client, s, call="QQ9ZZZ", time_on="2026-10-12T11:00")
    r = client.post("/settings/data", data={
        "_csrf": csrf(client), "action": "cty_upload",
        "file": (io.BytesIO(b"not a list"), "cty.csv")}, content_type="multipart/form-data")
    assert r.status_code == 302 and cty.installed_file(app.config["PATHS"].data_dir) is None
    client.post("/settings/data", data={
        "_csrf": csrf(client), "action": "cty_upload",
        "file": (io.BytesIO(fake_list()), "cty.csv")}, content_type="multipart/form-data")
    page = client.get("/settings/data").text
    assert "151" in page and "2 QSOs in the log have no country" in page
    client.post("/settings/data", data={"_csrf": csrf(client), "action": "cty_fill"})
    s.expire_all()
    assert {q.call: q.country for q in s.query(Qso)} == {"IK2ABC": "Italy", "QQ9ZZZ": None}
    assert any("before-country-fill" in p.name for p in app.config["PATHS"].backups.iterdir())
    client.post("/settings/data", data={"_csrf": csrf(client), "action": "cty_auto", "enabled": "0"})
    assert "Download it every month" in client.get("/settings/data").text


def test_countries_in_statistics(app, client, s):
    _install(app)
    _post_qso(client, s)
    page = client.get("/stats/").text
    assert "countries worked" in page and "QSOs by country" in page
    assert "/qsos/?country=Italy" in page
