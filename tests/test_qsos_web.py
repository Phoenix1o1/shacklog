"""QSO section: form, enrichment, filters, worked-before, soft delete."""

from shacklog.models import Qso, Station
from tests.conftest import csrf


def _post_qso(client, s, **overrides):
    station = s.query(Station).filter_by(name="Milano").one()
    data = {
        "_csrf": csrf(client), "station_id": station.id, "call": "ik2abc",
        "time_on": "2026-10-12T10:05", "freq_mhz": "145,500", "mode": "FM",
        "rst_sent": "59", "rst_rcvd": "57", "locator": "jn45nm", "name": "Marco",
        "prop_mode": "", "qsl_sent": "", "qsl_rcvd": "", "lotw_sent": "", "lotw_rcvd": "",
        "eqsl_sent": "", "eqsl_rcvd": "",
    }
    data.update(overrides)
    return client.post("/qsos/new", data=data)


def test_new_qso_is_enriched(client, s):
    r = _post_qso(client, s)
    assert r.status_code == 302
    q = s.query(Qso).one()
    assert q.call == "IK2ABC"
    assert q.band == "2m" and q.freq_mhz == 145.5
    assert q.my_call == "IU2TST" and q.my_locator == "JN45ol"
    assert q.distance_km is not None and q.distance_km < 20
    assert q.origin_instance and q.change_seq > 0


def test_khz_and_dmr_are_normalised(client, s):
    _post_qso(client, s, call="F1XYZ", freq_mhz="430500", mode="DMR", locator="JN25vd")
    q = s.query(Qso).one()
    assert q.freq_mhz == 430.5 and q.band == "70cm"
    assert (q.mode, q.submode) == ("DIGITALVOICE", "DMR")


def test_filters_near_station_and_callsign(client, s):
    _post_qso(client, s)
    _post_qso(client, s, call="F1XYZ", locator="JN25vd")
    client.get("/")  # consume the "Logged …" flash messages
    near_grenoble = client.get("/qsos/?near=Grenoble&radius=50").text
    assert "F1XYZ" in near_grenoble and "IK2ABC" not in near_grenoble
    by_call = client.get("/qsos/?call=IK2*").text
    assert "IK2ABC" in by_call and "F1XYZ" not in by_call


def test_validation_returns_422(client, s):
    r = _post_qso(client, s, call="", locator="ZZ99")
    assert r.status_code == 422
    assert "Not a Maidenhead locator" in r.text


def test_worked_before_hint(client, s):
    _post_qso(client, s)
    hint = client.get("/qsos/check-call?call=ik2abc").text
    assert "Worked" in hint and "IK2ABC" in hint
    assert "New callsign" in client.get("/qsos/check-call?call=ON4NEW").text


def test_delete_is_soft_and_needs_csrf(client, s):
    _post_qso(client, s)
    q = s.query(Qso).one()
    assert client.post(f"/qsos/{q.id}/delete").status_code == 400
    assert client.post(f"/qsos/{q.id}/delete", data={"_csrf": csrf(client)}).status_code == 302
    s.expire_all()
    assert s.get(Qso, q.id).deleted_at is not None
    assert client.get(f"/qsos/{q.id}").status_code == 404
