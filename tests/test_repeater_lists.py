"""Repeater lists from RepeaterBook and other tables (services/repeater_lists.py)."""

from pathlib import Path

import pytest

from shacklog.services import repeater_lists as rl

DATA = Path(__file__).parent / "data"


def _read(name):
    table = rl.read_table(rl.decode((DATA / name).read_bytes()))
    return table, rl.rows_to_repeaters(table, rl.guess_mapping(table.headers, table.rows))


def test_repeaterbook_json():
    table, result = _read("repeaterbook.json")
    assert table.source == "RepeaterBook JSON"
    fm, dmr = result.repeaters
    assert fm["callsign"] == "IR2UFD" and fm["output_mhz"] == 145.6125
    assert fm["input_mhz"] == 145.0125 and fm["ctcss_up"] == 88.5 and fm["kind"] == "FM"
    assert (fm["latitude"], fm["longitude"]) == (45.8689, 8.7676)
    assert fm["location_name"] == "Milano" and fm["region"] == "Lombardia"
    assert fm["status"] == "active" and str(fm["last_verified"]) == "2026-05-01"
    assert fm["name"] == "IR2UFD Milano"
    assert dmr["kind"] == "DMR" and dmr["color_code"] == 1 and dmr["notes"] == "BrandMeister"
    assert result.errors == ["Row 3: no output frequency, skipped."]


def test_italian_semicolon_list_with_title_line():
    table, result = _read("ponti_it.csv")
    mapping = rl.guess_mapping(table.headers, table.rows)
    assert {table.headers[i]: f for i, f in mapping.items()} == {
        "Nominativo": "callsign", "Frequenza": "output_mhz", "Shift": "shift",
        "Tono": "ctcss_up", "Modo": "kind", "Località": "location_name",
        "Regione": "region", "Locatore": "locator", "Note": "notes"}
    ufd, ugm, ugw = result.repeaters
    assert ufd["input_mhz"] == pytest.approx(145.0125) and ufd["ctcss_up"] == 88.5
    assert ufd["locator"] == "JN45nu" and ufd["region"] == "Lombardia"
    assert ugm["input_mhz"] == pytest.approx(438.5125) and ugm["kind"] == "DMR"
    assert ugm["color_code"] == 1
    assert ugw["input_mhz"] == pytest.approx(145.175)  # "-": the usual 2 m shift
    assert ugw["dcs_code"] == "023" and "ctcss_up" not in ugw


@pytest.mark.parametrize("text, output, expected", [
    ("-600", 145.6, -0.6), ("-0,6", 145.6, -0.6), ("+7.6 MHz", 430.9, 7.6),
    ("145.000", 145.6, -0.6), ("-", 430.9, -7.6), ("simplex", 145.5, 0.0), ("", 145.5, None),
])
def test_shift(text, output, expected):
    value = rl.shift_mhz(text, output)
    assert value == (pytest.approx(expected) if expected is not None else None)


def test_values():
    assert rl.frequency_mhz("145,6125 MHz") == 145.6125
    assert rl.frequency_mhz("430 900") == 430.9
    assert rl.frequency_mhz("145612500") == 145.6125  # Hz
    assert rl.tone("CTCSS 123,0") == (123.0, None) and rl.tone("023N") == (None, "023")
    assert rl.mode("YSF / C4FM") == "C4FM" and rl.mode("D-STAR DV") == "DSTAR"
    assert rl.coordinate("9.18 W") == -9.18 and rl.coordinate("45,5N") == 45.5


def test_not_a_list():
    with pytest.raises(ValueError):
        rl.read_table("just one line")
    with pytest.raises(ValueError):
        rl.read_table('{"hello": 1}')
