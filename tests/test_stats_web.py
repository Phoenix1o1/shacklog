"""Statistics: empty page, totals, bars, ODX per band, repeaters, year/station filters."""

from datetime import datetime

from shacklog.models import Equipment, Qso, Repeater, Station, SwlEntry


def test_empty_page(client):
    r = client.get("/stats/")
    assert r.status_code == 200 and "Statistics appear once" in r.text
    assert client.get("/stats/?year=1999&station=nope").status_code == 200
    assert client.get("/stats/?year=abc").status_code == 200


def _seed(s):
    milano = s.query(Station).filter_by(name="Milano").one()
    grenoble = s.query(Station).filter_by(name="Grenoble").one()
    radio = Equipment(name="TM-733E", kind="radio")
    rpt = Repeater(name="Milano R1", output_mhz=145.6, input_mhz=145.0)
    s.add_all([radio, rpt])
    s.flush()
    rows = [
        ("IK2ABC", datetime(2026, 3, 1, 10), "2m", "FM", None, 12.0, milano, rpt),
        ("IK2ABC", datetime(2026, 4, 2, 10), "2m", "FM", None, 12.0, milano, rpt),
        ("F1XYZ", datetime(2026, 5, 3, 10), "2m", "DIGITALVOICE", "DMR", 270.0, grenoble, None),
        ("ON4AAA", datetime(2025, 6, 4, 10), "70cm", "FM", None, 700.0, milano, None),
        ("ON4BBB", datetime(2025, 7, 4, 10), "70cm", "SSB", "USB", 650.0, milano, None),
    ]
    for call, when, band, mode, sub, dist, st, via in rows:
        s.add(Qso(call=call, time_on=when, band=band, mode=mode, submode=sub, distance_km=dist,
                  station_id=st.id, radio_id=radio.id, repeater_id=via.id if via else None))
    s.add(Qso(call="DELETED", time_on=datetime(2026, 1, 1), band="2m", mode="FM",
              deleted_at=datetime(2026, 1, 2)))
    s.add_all([SwlEntry(time_utc=datetime(2026, 2, 1), heard="NOAA 19", category="weather"),
               SwlEntry(time_utc=datetime(2026, 2, 2), heard="noaa 19", category="weather"),
               SwlEntry(time_utc=datetime(2026, 2, 3), heard="Linate", category="airband")])
    s.commit()
    return milano, grenoble


def test_totals_and_sections(client, s):
    _seed(s)
    page = client.get("/stats/").text
    assert "<b>5</b><span>QSOs</span>" in page
    assert "<b>4</b><span>different callsigns worked</span>" in page
    assert "<b>3</b><span>listening entries</span>" in page
    assert "<b>2</b><span>different things heard</span>" in page  # case-insensitive
    assert "DELETED" not in page
    assert 'title="2m"' in page and 'title="70cm"' in page
    assert page.index('title="2m"') < page.index('title="70cm"')  # band order
    assert 'title="DMR"' in page and 'title="USB"' in page  # submode shown, like the log
    assert 'style="width: 100.0%"' in page
    assert "Milano R1" in page and "TM-733E" in page
    assert 'title="Weather satellite / sonde"' in page
    assert "QSOs per month, last 24 months" in page and 'title="2026-03"' in page


def test_odx_links_the_longest_qso_per_band(client, s):
    _seed(s)
    page = client.get("/stats/").text
    odx = page[page.index("Longest distance per band"):page.index("Most worked callsigns")]
    assert "F1XYZ" in odx and "ON4AAA" in odx and "ON4BBB" not in odx and "IK2ABC" not in odx
    f1 = s.query(Qso).filter_by(call="F1XYZ").one()
    assert f"/qsos/{f1.id}" in odx


def test_year_and_station_filters(client, s):
    milano, grenoble = _seed(s)
    y2025 = client.get("/stats/?year=2025").text
    assert "<b>2</b><span>QSOs</span>" in y2025 and "Most worked" in y2025
    assert "QSOs by year" not in y2025
    gre = client.get(f"/stats/?station={grenoble.id}").text
    assert "<b>1</b><span>QSOs</span>" in gre and "F1XYZ" in gre and "ON4AAA" not in gre
    none = client.get(f"/stats/?year=2020&station={milano.id}").text
    assert "Nothing logged for this selection" in none
