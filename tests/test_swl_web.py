"""Listening log: form, category guess, sticky fields, filters, CSV export, soft delete."""

import csv
import io

from shacklog.models import Equipment, Repeater, Station, SwlEntry
from shacklog.web.views.swl import parse_frequency_text
from tests.conftest import csrf


def _post(client, s, **overrides):
    station = s.query(Station).filter_by(name="Milano").one()
    data = {"_csrf": csrf(client), "station_id": station.id, "time_utc": "2026-10-12T10:05",
            "freq_mhz": "145,500", "mode": "fm", "category": "", "heard": "ik2abc",
            "signal": "S7", "locator": "jn45nm"}
    data.update(overrides)
    return client.post("/swl/new", data=data)


def test_frequency_text_is_read_for_listeners():
    assert parse_frequency_text("137.100") == 137.1  # a NOAA pass is not 137 kHz
    assert parse_frequency_text("118,7") == 118.7
    assert parse_frequency_text("145500") == 145.5  # kHz
    assert parse_frequency_text("7074") == 7.074
    assert parse_frequency_text("1090 MHz") == 1090.0
    assert parse_frequency_text("7074 kHz") == 7.074
    assert parse_frequency_text("JN45") is None
    assert parse_frequency_text("") is None


def test_new_entry_is_enriched(client, s):
    r = _post(client, s)
    assert r.status_code == 302
    e = s.query(SwlEntry).one()
    assert e.heard == "IK2ABC"  # amateur: callsign casing
    assert e.category == "amateur" and e.band == "2m" and e.freq_mhz == 145.5
    assert e.mode == "FM"
    assert e.latitude is not None and e.my_latitude is not None
    assert e.distance_km is not None and e.distance_km < 20
    assert r.headers["Location"].endswith(f"/swl/{e.id}")
    page = client.get(f"/swl/{e.id}").text
    assert "IK2ABC" in page and "Recordings, screenshots" in page


def test_category_is_guessed_and_heard_keeps_case(client, s):
    _post(client, s, freq_mhz="137.1", mode="APT", heard="NOAA 19", locator="")
    _post(client, s, freq_mhz="118.700", mode="AM", heard="Linate Tower", locator="")
    weather, air = s.query(SwlEntry).order_by(SwlEntry.freq_mhz.desc()).all()
    assert (weather.category, weather.heard, weather.freq_mhz) == ("weather", "NOAA 19", 137.1)
    assert (air.category, air.heard) == ("airband", "Linate Tower")


def test_picked_category_is_kept(client, s):
    _post(client, s, freq_mhz="145.800", category="satellite", heard="ISS")
    e = s.query(SwlEntry).one()
    assert e.category == "satellite" and e.heard == "ISS"


def test_validation_returns_422(client, s):
    r = _post(client, s, time_utc="", locator="ZZ99", freq_mhz="abc")
    assert r.status_code == 422
    assert "Not a Maidenhead locator" in r.text and "Enter a frequency" in r.text
    r = _post(client, s, time_end="2026-10-12T09:00")
    assert r.status_code == 422 and "end is before the start" in r.text
    assert s.query(SwlEntry).count() == 0


def test_new_form_defaults_to_station_radio(client, s):
    sdr = Equipment(name="RTL-SDR v4", kind="receiver")
    s.add(sdr)
    s.flush()
    s.query(Station).filter_by(name="Milano").one().radio_id = sdr.id
    s.commit()
    page = client.get("/swl/new").text
    assert f'<option value="{sdr.id}" selected>' in page


def test_log_and_start_another_keeps_sticky_fields(client, s):
    sdr = Equipment(name="RTL-SDR v4", kind="receiver")
    s.add(sdr)
    s.commit()
    r = _post(client, s, again="1", receiver_id=sdr.id, freq_mhz="137.9125", mode="LRPT",
              category="weather", heard="Meteor M2-3", locator="")
    assert "from=" in r.headers["Location"]
    page = client.get(r.headers["Location"]).text
    assert 'value="137.9125"' in page and 'value="LRPT"' in page
    assert f'<option value="{sdr.id}" selected>' in page
    assert '<option value="weather" selected>' in page
    assert "Logged Meteor M2-3." in page
    assert 'value="Meteor M2-3"' not in page  # "heard" is not sticky


def test_repeater_fills_frequency_and_heard(client, s):
    rpt = Repeater(name="Milano R1", callsign="IR2UFD", output_mhz=145.6, input_mhz=145.0,
                   latitude=45.5, longitude=9.2)
    s.add(rpt)
    s.commit()
    _post(client, s, freq_mhz="", heard="", locator="", repeater_id=rpt.id)
    e = s.query(SwlEntry).one()
    assert e.freq_mhz == 145.6 and e.heard == "IR2UFD" and e.category == "repeater"
    assert e.latitude == 45.5


def test_edit_and_soft_delete(client, s):
    _post(client, s)
    e = s.query(SwlEntry).one()
    data = {"_csrf": csrf(client), "time_utc": "2026-10-12T11:00", "freq_mhz": "430.500",
            "mode": "FM", "category": "amateur", "heard": "f1xyz", "station_id": e.station_id}
    assert client.post(f"/swl/{e.id}/edit", data=data).status_code == 302
    s.expire_all()
    e = s.get(SwlEntry, e.id)
    assert e.heard == "F1XYZ" and e.band == "70cm"
    assert client.post(f"/swl/{e.id}/delete").status_code == 400  # CSRF
    assert client.post(f"/swl/{e.id}/delete", data={"_csrf": csrf(client)}).status_code == 302
    s.expire_all()
    assert s.get(SwlEntry, e.id).deleted_at is not None
    assert client.get(f"/swl/{e.id}").status_code == 404


def test_filters(client, s):
    _post(client, s)  # IK2ABC 145.5 near Milano
    _post(client, s, heard="F1XYZ", locator="JN25vd", freq_mhz="430.500")
    _post(client, s, heard="NOAA 19", freq_mhz="137.1", locator="", mode="APT")
    client.get("/")  # consume flashes
    near = client.get("/swl/?near=Grenoble&radius=50").text
    assert "F1XYZ" in near and "IK2ABC" not in near
    assert "IK2ABC" in client.get("/swl/?heard=IK2*").text
    weather = client.get("/swl/?category=weather").text
    assert "NOAA 19" in weather and "IK2ABC" not in weather
    band = client.get("/swl/?band=70cm").text
    assert "F1XYZ" in band and "IK2ABC" not in band
    rng = client.get("/swl/?fmin=137&fmax=138").text
    assert "NOAA 19" in rng and "F1XYZ" not in rng
    assert "NOAA 19" in client.get("/swl/?mode=APT").text
    assert "No" in client.get("/swl/?from=2027-01-01").text


def test_csv_export_has_all_fields(client, s):
    _post(client, s, notes="Strong signal", tags="sdr, test")
    r = client.get("/swl/export.csv?category=amateur")
    assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 1
    row = rows[0]
    for col in ("time_utc", "freq_mhz", "category", "heard", "signal", "locator", "latitude",
                "distance_km", "notes", "tags", "station", "receiver", "antenna", "repeater"):
        assert col in row
    assert row["heard"] == "IK2ABC" and row["station"] == "Milano" and row["notes"] == "Strong signal"


def test_pages_on_empty_db(client):
    assert client.get("/swl/").status_code == 200
    assert client.get("/swl/new").status_code == 200
    assert client.get("/swl/export.csv").status_code == 200
    assert client.get("/swl/does-not-exist").status_code == 404
