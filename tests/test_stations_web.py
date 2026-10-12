"""Stations section: form, locator ↔ coordinates, default/reference settings, nearest repeaters."""

from shacklog.db import get_setting
from shacklog.models import Equipment, Qso, Repeater, Station
from shacklog.services import geo
from tests.conftest import csrf


def _post(client, url, **fields):
    data = {"_csrf": csrf(client), "kind": "portable"}
    data.update(fields)
    return client.post(url, data=data)


def test_create_with_locator_fills_coordinates(client, s):
    r = _post(client, "/stations/new", name="Monte Penice", locator="jn44ns", callsign="iu2tst/p")
    assert r.status_code == 302
    st = s.query(Station).filter_by(name="Monte Penice").one()
    assert st.locator == "JN44ns" and st.callsign == "IU2TST/P"
    assert st.latitude is not None and st.longitude is not None
    assert geo.latlon_to_locator(st.latitude, st.longitude, 6) == "JN44ns"


def test_create_with_coordinates_fills_locator(client, s):
    _post(client, "/stations/new", name="Car", latitude="45,188", longitude="5.724")
    st = s.query(Station).filter_by(name="Car").one()
    assert st.latitude == 45.188 and st.locator == geo.latlon_to_locator(45.188, 5.724, 6)


def test_validation(client):
    r = _post(client, "/stations/new", name="", latitude="45.1")
    assert r.status_code == 422
    assert "Required" in r.text and "Give both latitude and longitude" in r.text
    r = _post(client, "/stations/new", name="X", locator="ZZ99")
    assert r.status_code == 422 and "Not a Maidenhead locator" in r.text


def test_edit_locator_moves_coordinates(client, s):
    st = s.query(Station).filter_by(name="Grenoble").one()
    old_lat = st.latitude
    r = _post(client, f"/stations/{st.id}/edit", name="Grenoble", kind="fixed",
              locator="JN35ab", latitude=str(st.latitude), longitude=str(st.longitude))
    assert r.status_code == 302
    s.expire_all()
    st = s.get(Station, st.id)
    assert st.locator == "JN35ab" and st.latitude != old_lat
    assert geo.latlon_to_locator(st.latitude, st.longitude, 6) == "JN35ab"


def test_default_radio_and_antenna_selects(client, s):
    radio = Equipment(name="TM-733E", kind="radio")
    ant = Equipment(name="Diamond X-50", kind="antenna")
    s.add_all([radio, ant])
    s.commit()
    form = client.get("/stations/new").text
    assert "TM-733E" in form and "Diamond X-50" in form
    _post(client, "/stations/new", name="Shack 2", radio_id=radio.id, antenna_id=ant.id, power_w="25")
    st = s.query(Station).filter_by(name="Shack 2").one()
    assert st.radio_id == radio.id and st.antenna_id == ant.id and st.power_w == 25
    page = client.get(f"/stations/{st.id}").text
    assert "TM-733E" in page and "Diamond X-50" in page


def test_default_and_reference_buttons(client, s, db):
    grenoble = s.query(Station).filter_by(name="Grenoble").one()
    page = client.get(f"/stations/{grenoble.id}").text
    assert "Use as default for new entries" in page and "Use as distance reference" in page
    assert client.post(f"/stations/{grenoble.id}/make-default", data={"_csrf": csrf(client)}).status_code == 302
    assert client.post(f"/stations/{grenoble.id}/make-reference", data={"_csrf": csrf(client)}).status_code == 302
    s.expire_all()
    assert get_setting(s, "default_station_id") == grenoble.id
    assert get_setting(s, "reference_station_id") == grenoble.id
    page = client.get(f"/stations/{grenoble.id}").text
    assert "Use as default for new entries" not in page
    # The station list now measures distances from Grenoble.
    assert "km from Grenoble" in client.get("/stations/").text


def test_view_counts_and_nearest_repeaters(client, s):
    milano = s.query(Station).filter_by(name="Milano").one()
    lat, lon = geo.locator_to_latlon("JN45nm")
    s.add_all([
        Repeater(name="Near Milano", callsign="IR2AAA", output_mhz=145.6, input_mhz=145.0,
                 latitude=lat, longitude=lon, locator="JN45nm"),
        Repeater(name="Grenoble rpt", callsign="F1ZZZ", output_mhz=145.7, input_mhz=145.1,
                 latitude=45.19, longitude=5.72),
        Repeater(name="No position", output_mhz=430.1),
    ])
    from datetime import datetime
    s.add(Qso(call="IK2ABC", time_on=datetime(2026, 10, 1, 10, 0), station_id=milano.id))
    s.commit()
    page = client.get(f"/stations/{milano.id}").text
    assert "Nearest repeaters" in page
    assert page.index("IR2AAA") < page.index("F1ZZZ")
    assert "No position" not in page
    assert f"/qsos/?station={milano.id}" in page and "IK2ABC" in page
    assert "km" in page


def test_delete_is_soft_and_clears_settings(client, s):
    milano = s.query(Station).filter_by(name="Milano").one()
    r = client.post(f"/stations/{milano.id}/delete", data={"_csrf": csrf(client)})
    assert r.status_code == 302
    s.expire_all()
    assert s.get(Station, milano.id).deleted_at is not None
    assert get_setting(s, "default_station_id") is None
    assert client.get(f"/stations/{milano.id}").status_code == 404
    assert "Milano" not in client.get("/stations/").text


def test_pages_render(client, s):
    for url in ("/stations/", "/stations/new", "/stations/?q=gren", "/stations/?kind=fixed",
                "/stations/?sort=distance", "/stations/?sort=name&dir=desc"):
        assert client.get(url).status_code == 200, url
    for st in s.query(Station).all():
        assert client.get(f"/stations/{st.id}").status_code == 200
        assert client.get(f"/stations/{st.id}/edit").status_code == 200
    assert client.get("/stations/does-not-exist").status_code == 404
