"""Locators, distances, position parsing, bands and modes."""

import pytest

from shacklog.services import bands, geo


def test_locator_roundtrip():
    lat, lon = geo.locator_to_latlon("JN45ol")
    assert 45.4 < lat < 45.5 and 9.1 < lon < 9.25
    assert geo.latlon_to_locator(lat, lon, 6) == "JN45ol"
    assert geo.latlon_to_locator(45.464, 9.19, 4) == "JN45"
    assert geo.latlon_to_locator(45.464, 9.19, 10)[:6] == "JN45ol"


def test_locator_validation():
    assert geo.is_valid_locator("jn45")
    assert not geo.is_valid_locator("JN4")
    assert not geo.is_valid_locator("ZZ99")
    with pytest.raises(ValueError):
        geo.locator_to_latlon("hello")
    assert geo.normalize_locator("jn45OL") == "JN45ol"


def test_distance_milano_grenoble():
    d = geo.haversine_km(45.464, 9.19, 45.188, 5.724)
    assert 265 < d < 280
    b = geo.bearing_deg(45.464, 9.19, 45.188, 5.724)
    assert 255 < b < 270 and geo.compass_point(b) == "W"


def test_parse_position():
    assert geo.parse_position("45.464, 9.19") == (45.464, 9.19)
    assert geo.parse_position("45,464 9,19") is None or True  # ambiguous; must not crash
    lat, lon = geo.parse_position("45°27'51\"N 9°11'25\"E")
    assert abs(lat - 45.464) < 0.01 and abs(lon - 9.19) < 0.01
    assert geo.parse_position("JN45") is not None
    assert geo.parse_position("nonsense") is None


def test_bounding_box_contains_circle():
    min_lat, min_lon, max_lat, max_lon = geo.bounding_box(45.4, 9.2, 100)
    assert min_lat < 45.4 < max_lat and min_lon < 9.2 < max_lon
    assert geo.bounding_box(89.9, 0, 50)[1] == -180.0


def test_bands_and_modes():
    assert bands.band_for_freq(145.5) == "2m"
    assert bands.band_for_freq(433.5) == "70cm"
    assert bands.band_for_freq(121.5) is None
    assert bands.normalize_mode("dmr") == ("DIGITALVOICE", "DMR")
    assert bands.normalize_mode("usb") == ("SSB", "USB")
    assert bands.service_hint(121.5)[0] == "airband"
    assert bands.service_hint(446.00625)[0] == "pmr"
    assert bands.service_hint(145.5)[0] == "amateur"
    assert bands.default_rst("CW") == "599"
