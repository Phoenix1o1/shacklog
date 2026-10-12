"""Map: page config, JSON layers with each section's filters, point cap, popups' data."""

import json
import re

from shacklog.models import Qso, Repeater, Station, SwlEntry, utcnow
from shacklog.services import geo
from shacklog.web.views import map as map_view


def _config(html: str) -> dict:
    block = re.search(r'<script type="application/json" id="map-config">(.*?)</script>', html, re.S)
    return json.loads(block.group(1))


def _seed(s):
    lat, lon = geo.locator_to_latlon("JN45ol")
    glat, glon = geo.locator_to_latlon("JN25vd")
    s.add_all([
        Qso(call="IK2ABC", time_on=utcnow(), band="2m", mode="FM", latitude=45.4, longitude=9.1,
            my_latitude=lat, my_longitude=lon, distance_km=10.0),
        Qso(call="F1XYZ", time_on=utcnow(), band="70cm", mode="FM", latitude=glat, longitude=glon),
        Qso(call="NOPOS", time_on=utcnow(), band="2m", mode="FM"),
        SwlEntry(time_utc=utcnow(), heard="NOAA 19", category="weather", freq_mhz=137.1,
                 latitude=46.0, longitude=10.0),
        SwlEntry(time_utc=utcnow(), heard="Tower", category="airband", freq_mhz=118.7),
        Repeater(name="Milano R1", callsign="IR2UFD", output_mhz=145.6, input_mhz=145.0,
                 latitude=45.5, longitude=9.2),
        Repeater(name="Locator only", output_mhz=430.5, locator="JN45"),
        Repeater(name="Nowhere", output_mhz=431.0),
    ])
    s.commit()


def test_page_renders_on_empty_db(client):
    r = client.get("/map")
    assert r.status_code == 200 and client.get("/map/").status_code == 200
    cfg = _config(r.text)
    assert cfg["tiles"]["url"] == "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    assert "OpenStreetMap" in cfg["tiles"]["attribution"]
    assert [layer["key"] for layer in cfg["layers"]] == ["qso", "swl", "repeater", "station"]
    assert all(layer["on"] for layer in cfg["layers"])
    assert cfg["home"]["name"] == "Milano"
    assert "need an internet connection" in r.text
    assert "vendor/leaflet/leaflet.js" in r.text and "js/map.js" in r.text
    for key in ("qso", "swl", "repeater", "station"):
        assert client.get(f"/map/data.json?layer={key}").status_code == 200


def test_unknown_layer_is_rejected(client):
    assert client.get("/map/data.json").status_code == 400
    assert client.get("/map/data.json?layer=nope").status_code == 400


def test_qso_layer_with_filters(client, s):
    _seed(s)
    data = client.get("/map/data.json?layer=qso").get_json()
    assert data["shown"] == 2 and data["without_position"] == 1 and not data["capped"]
    ik2 = next(p for p in data["points"] if p["title"] == "IK2ABC")
    assert ik2["url"].startswith("/qsos/") and ik2["my"] is not None and "2m" in ik2["sub"]
    filtered = client.get("/map/data.json?layer=qso&call=F1*").get_json()
    assert [p["title"] for p in filtered["points"]] == ["F1XYZ"]
    assert filtered["active"][0][0] == "call"
    near = client.get("/map/data.json?layer=qso&near=Grenoble&radius=30").get_json()
    assert [p["title"] for p in near["points"]] == ["F1XYZ"]
    bbox = client.get("/map/data.json?layer=qso&bbox=45,9,46,10").get_json()
    assert [p["title"] for p in bbox["points"]] == ["IK2ABC"]


def test_swl_layer_with_filters(client, s):
    _seed(s)
    data = client.get("/map/data.json?layer=swl").get_json()
    assert [p["title"] for p in data["points"]] == ["NOAA 19"] and data["without_position"] == 1
    assert client.get("/map/data.json?layer=swl&category=airband").get_json()["shown"] == 0


def test_repeater_layer_uses_locator_when_no_coordinates(client, s):
    _seed(s)
    data = client.get("/map/data.json?layer=repeater").get_json()
    titles = sorted(p["title"] for p in data["points"])
    assert titles == ["Locator only", "Milano R1"]
    loc_only = next(p for p in data["points"] if p["title"] == "Locator only")
    assert (loc_only["lat"], loc_only["lon"]) == (45.5, 9.0)  # centre of JN45
    milano = next(p for p in data["points"] if p["title"] == "Milano R1")
    assert "145.6000 MHz" in milano["sub"] and "-0.6" in milano["sub"]


def test_station_layer(client, s):
    data = client.get("/map/data.json?layer=station").get_json()
    assert sorted(p["title"] for p in data["points"]) == ["Grenoble", "Milano"]
    assert all(p["url"] for p in data["points"])
    s.add(Station(name="Somewhere", kind="portable"))  # no position: not drawn
    s.commit()
    assert client.get("/map/data.json?layer=station").get_json()["without_position"] == 1


def test_points_are_capped(client, s, monkeypatch):
    monkeypatch.setattr(map_view, "MAX_POINTS", 2)
    for i in range(3):
        s.add(Qso(call=f"IK2A{chr(65 + i)}", time_on=utcnow(), latitude=45.0 + i, longitude=9.0))
    s.commit()
    data = client.get("/map/data.json?layer=qso").get_json()
    assert data["capped"] and data["shown"] == 2 and data["total"] == 3 and data["max"] == 2


def test_page_with_layer_and_filters(client, s):
    _seed(s)
    r = client.get("/map?layer=qso&call=IK2*&near=Milano&radius=25")
    cfg = _config(r.text)
    layers = {layer["key"]: layer for layer in cfg["layers"]}
    assert layers["qso"]["on"] and not layers["swl"]["on"]
    assert "call=IK2%2A" in layers["qso"]["filters"] and "layer=" not in layers["qso"]["filters"]
    assert layers["swl"]["filters"] == ""
    assert layers["qso"]["listUrl"] == "/qsos/" and layers["swl"]["listUrl"] == "/swl/"
    assert cfg["focus"]["radius_km"] == 25 and cfg["focus"]["label"] == "Milano"
    assert "Back to the list" in r.text and "/qsos/?call=IK2" in r.text
    assert "Callsign: <b>IK2*</b>" in r.text
    assert "List what" in r.text
