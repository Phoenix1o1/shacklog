"""ADI parsing/writing and the ADIF ↔ Qso field mapping (services/adif.py)."""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from shacklog import __version__
from shacklog.services import adif, geo

DATA = Path(__file__).parent / "data" / "formats"


def _qso(**kw):
    """A stand-in for a Qso row: every mapped attribute defaults to None."""
    attrs = {attr: None for _, attr, _ in adif.FIELD_MAP}
    attrs.update(time_on=None, time_off=None, extra_adif=None, id=None, station=None,
                 distance_km=None)
    attrs.update(kw)
    return SimpleNamespace(**attrs)


# ---------------------------------------------------------------------------
# parse_adi
# ---------------------------------------------------------------------------

def test_parse_sample_file():
    header, records = adif.parse_adi((DATA / "sample.adi").read_bytes())
    assert header["adif_ver"] == "3.1.4" and header["programid"] == "TestLog"
    assert len(records) == 7
    first = records[0]
    assert first["call"] == "IK2ABC" and first["qso_date"] == "20261011"
    assert first["time_on"] == "0805"  # type indicator ":T" is ignored
    assert first["qth"] == "Città Studi"  # byte-counted length recovered
    assert first["app_testlog_rig"] == "TM-733"
    assert records[1]["qth"] == "Grenoble–Échirolles"  # character-counted (spec)
    assert records[1]["call"] == "F1ZCQ"  # lower-case field names in the file
    assert records[-1]["sat_name"] == "ISS"  # last record without <EOR>
    assert "free text" not in " ".join(records[0].values())


def test_no_header_when_file_starts_with_tag():
    header, records = adif.parse_adi("<CALL:5>IK2AB<QSO_DATE:8>20261012<TIME_ON:4>1005<eor>")
    assert header == {}
    assert records == [{"call": "IK2AB", "qso_date": "20261012", "time_on": "1005"}]


def test_header_without_leading_text():
    header, records = adif.parse_adi("<ADIF_VER:5>3.1.5<EoH><CALL:5>IK2AB<EoR>")
    assert header == {"adif_ver": "3.1.5"}
    assert records == [{"call": "IK2AB"}]


def test_comments_crlf_and_case_insensitive_markers():
    text = ("Header text\r\n<eoh>\r\n"
            "<call:5>IK2AB  some comment <Mode:2>FM\r\n<eor>\r\n"
            "junk < not a tag <CALL:5>F1ABC <EOR>")
    _, records = adif.parse_adi(text)
    assert records == [{"call": "IK2AB", "mode": "FM"}, {"call": "F1ABC"}]


@pytest.mark.parametrize("value", ["Città", "Grenoble–Échirolles"])
def test_length_in_characters_or_utf8_bytes(value):
    chars = f"<QTH:{len(value)}>{value}<CALL:5>F1ABC<EOR>"
    nbytes = f"<QTH:{len(value.encode())}>{value}<CALL:5>F1ABC<EOR>"
    spaced = f"<QTH:{len(value.encode())}>{value} <CALL:5>F1ABC <EOR>"
    for text in (chars, nbytes, spaced):
        for data in (text, text.encode("utf-8")):
            _, records = adif.parse_adi(data)
            assert records == [{"qth": value, "call": "F1ABC"}], text


def test_byte_length_value_followed_by_text_on_same_line():
    # Byte count lands on the space before more free text; char count would eat "xy".
    value = "Città"
    _, records = adif.parse_adi(f"<QTH:6>{value} xy <CALL:5>F1ABC<EOR>")
    assert records[0]["qth"] == value


def test_latin1_fallback():
    data = "<CALL:5>IK2AB<QTH:5>Città<EOR>".encode("latin-1")
    _, records = adif.parse_adi(data)
    assert records[0]["qth"] == "Città"


def test_zero_length_and_bom():
    _, records = adif.parse_adi("﻿<CALL:5>IK2AB<NAME:0><EOR>".encode("utf-8"))
    assert records == [{"call": "IK2AB"}]


def test_empty_input():
    assert adif.parse_adi(b"") == ({}, [])
    assert adif.parse_adi("just some text, no tags") == ({}, [])


# ---------------------------------------------------------------------------
# write_adi
# ---------------------------------------------------------------------------

def test_write_header_and_character_lengths():
    text = adif.write_adi([{"call": "F1ABC", "qth": "Grenoble–Échirolles", "name": "", "x": None}])
    assert not text.startswith("<")  # free text first = header present
    assert "<ADIF_VER:5>3.1.5" in text and "<PROGRAMID:8>ShackLog" in text
    assert f"<PROGRAMVERSION:{len(__version__)}>{__version__}" in text
    stamp = text.split("<CREATED_TIMESTAMP:15>")[1][:15]
    assert datetime.strptime(stamp, "%Y%m%d %H%M%S")
    assert "<QTH:19>Grenoble–Échirolles" in text  # 19 characters, not 22 bytes
    assert "<NAME" not in text  # empty values skipped
    assert text.rstrip().endswith("<eor>")


def test_write_header_override():
    text = adif.write_adi([], header={"PROGRAMID": "Other", "my_note": "x"})
    assert "<PROGRAMID:5>Other" in text and "<MY_NOTE:1>x" in text


def test_round_trip_write_parse_write():
    header, records = adif.parse_adi((DATA / "sample.adi").read_bytes())
    once = adif.write_adi(records)
    header2, records2 = adif.parse_adi(once)
    assert records2 == records
    assert header2["programid"] == "ShackLog"
    twice = adif.write_adi(records2)
    # Identical apart from the creation timestamp.
    strip = lambda t: [ln for ln in t.splitlines() if "CREATED_TIMESTAMP" not in ln]  # noqa: E731
    assert strip(once) == strip(twice)


# ---------------------------------------------------------------------------
# record_to_qso_fields
# ---------------------------------------------------------------------------

def test_record_to_fields_full_mapping():
    _, records = adif.parse_adi((DATA / "sample.adi").read_bytes())
    fields, extras = adif.record_to_qso_fields(records[0])
    assert fields["call"] == "IK2ABC"
    assert fields["time_on"] == datetime(2026, 10, 11, 8, 5)
    assert fields["freq_mhz"] == 145.0125 and fields["freq_rx_mhz"] == 145.6125
    assert fields["band"] == "2m"
    assert fields["locator"] == "JN45ol" and fields["my_locator"] == "JN45ol"
    assert fields["my_call"] == "IU2TST" and fields["tx_power_w"] == 5.0
    assert fields["prop_mode"] == "RPT" and fields["qth"] == "Città Studi"
    assert extras == {"app_testlog_rig": "TM-733", "distance": "3.2"}


def test_record_to_fields_positions_and_time_off():
    _, records = adif.parse_adi((DATA / "sample.adi").read_bytes())
    fields, _ = adif.record_to_qso_fields(records[1])
    assert fields["time_on"] == datetime(2026, 10, 11, 17, 15, 30)
    assert fields["time_off"] == datetime(2026, 10, 11, 17, 20)
    assert fields["latitude"] == pytest.approx(45.149)
    assert fields["longitude"] == pytest.approx(5.719)
    assert fields["my_call"] == "F/IU2TST" and fields["comment"] == "first QSO from Grenoble"


def test_time_off_past_midnight_and_invalid_number():
    _, records = adif.parse_adi((DATA / "sample.adi").read_bytes())
    problems = []
    fields, extras = adif.record_to_qso_fields(records[2], problems)
    assert fields["time_on"] == datetime(2026, 10, 11, 23, 55)
    assert fields["time_off"] == datetime(2026, 10, 12, 0, 10)
    assert "tx_power_w" not in fields and extras["tx_pwr"] == "abc"
    assert problems and "TX_PWR" in problems[0]
    assert (fields["mode"], fields["submode"]) == ("DIGITALVOICE", "DMR")
    assert fields["qsl_sent"] == "Y" and fields["lotw_rcvd"] == "N"
    assert fields["sota_ref"] == "DM/BW-001"


def test_invalid_values_go_to_extras():
    rec = {"call": "ik2 abc", "qso_date": "20261399", "time_on": "2561", "freq": "lots",
           "gridsquare": "ZZ99", "lat": "X045 00.000", "app_shacklog_id": "abc",
           "app_shacklog_tags": "sota, portable", "my_rig": "TM-733"}
    fields, extras = adif.record_to_qso_fields(rec)
    assert fields == {"call": "IK2ABC", "tags": "sota, portable"}
    assert extras == {"qso_date": "20261399", "time_on": "2561", "freq": "lots",
                      "gridsquare": "ZZ99", "lat": "X045 00.000", "app_shacklog_id": "abc",
                      "my_rig": "TM-733"}


def test_location_parsing_and_formatting():
    assert adif.parse_location("N045 27.840", "lat") == pytest.approx(45.464)
    assert adif.parse_location("W009 11.400", "lon") == pytest.approx(-9.19)
    assert adif.parse_location("S033 52.000", "lat") == pytest.approx(-33.866667, abs=1e-6)
    assert adif.parse_location("E009 11.400", "lat") is None  # wrong hemisphere for axis
    assert adif.parse_location("N091 00.000", "lat") is None
    assert adif.parse_location("45.464", "lat") == 45.464  # decimal tolerated
    assert adif.format_location(45.464, "lat") == "N045 27.840"
    assert adif.format_location(-9.19, "lon") == "W009 11.400"
    assert adif.format_location(0.0, "lon") == "E000 00.000"
    # 59.9999 minutes rounds up into the next degree instead of writing "60.000".
    assert adif.format_location(9.9999999, "lon") == "E010 00.000"


def test_hhmmss_and_hhmm_times():
    f1, _ = adif.record_to_qso_fields({"call": "A1A", "qso_date": "20261012", "time_on": "1005"})
    f2, _ = adif.record_to_qso_fields({"call": "A1A", "qso_date": "20261012", "time_on": "100530"})
    assert f1["time_on"] == datetime(2026, 10, 12, 10, 5)
    assert f2["time_on"] == datetime(2026, 10, 12, 10, 5, 30)


# ---------------------------------------------------------------------------
# qso_to_record
# ---------------------------------------------------------------------------

def test_qso_to_record_inverse_and_extras():
    my_lat, my_lon = geo.locator_to_latlon("JN25vd")
    qso = _qso(
        id="1234-uuid", call="F1ZCQ", time_on=datetime(2026, 10, 11, 17, 15, 30),
        time_off=datetime(2026, 10, 11, 17, 20), freq_mhz=145.65, band="2m", mode="FM",
        latitude=45.149, longitude=5.719, locator="JN25vd", tx_power_w=5.0,
        my_call=None, my_locator="JN25vd", my_latitude=my_lat, my_longitude=my_lon,
        station=SimpleNamespace(callsign="F/IU2TST"), tags="grenoble, first",
        extra_adif=json.dumps({"app_testlog_rig": "TM-733", "freq": "stale", "distance": "3.2"}),
    )
    rec = adif.qso_to_record(qso)
    assert rec["call"] == "F1ZCQ" and rec["qso_date"] == "20261011" and rec["time_on"] == "171530"
    assert rec["qso_date_off"] == "20261011" and rec["time_off"] == "172000"
    assert rec["freq"] == "145.65" and rec["tx_pwr"] == "5"  # mapped value beats the extra
    assert rec["lat"] == "N045 08.940" and rec["lon"] == "E005 43.140"
    assert rec["station_callsign"] == "F/IU2TST"  # from the station
    assert "my_lat" not in rec  # just the centre of MY_GRIDSQUARE (derived), not exported
    assert rec["app_shacklog_id"] == "1234-uuid" and rec["app_shacklog_tags"] == "grenoble, first"
    assert rec["app_testlog_rig"] == "TM-733" and rec["distance"] == "3.2"


def test_qso_record_round_trip_through_mapping():
    _, records = adif.parse_adi((DATA / "sample.adi").read_bytes())
    for rec in records:
        fields, extras = adif.record_to_qso_fields(rec)
        if "time_on" not in fields:
            continue
        qso = _qso(**fields, extra_adif=json.dumps(extras) if extras else None)
        fields2, extras2 = adif.record_to_qso_fields(adif.qso_to_record(qso))
        assert fields2 == fields
        assert extras2 == extras


def test_damaged_extra_adif_does_not_block_export():
    rec = adif.qso_to_record(_qso(call="IK2ABC", time_on=datetime(2026, 1, 1), extra_adif="{oops"))
    assert rec["call"] == "IK2ABC"


def test_midnight_times_and_date_off_without_time_off():
    rec = {"call": "A1A", "qso_date": "20261012", "time_on": "0000", "time_off": "0000"}
    fields, extras = adif.record_to_qso_fields(rec)
    assert fields["time_on"] == fields["time_off"] == datetime(2026, 10, 12)
    assert extras == {}
    rec = {"call": "A1A", "qso_date": "20261012", "time_on": "2350", "qso_date_off": "20261013"}
    fields, extras = adif.record_to_qso_fields(rec)
    assert "time_off" not in fields and extras == {"qso_date_off": "20261013"}
    explicit = {"call": "A1A", "qso_date": "20261012", "time_on": "2350",
                "qso_date_off": "20261014", "time_off": "0100"}
    assert adif.record_to_qso_fields(explicit)[0]["time_off"] == datetime(2026, 10, 14, 1, 0)


def test_power_with_unit_and_decimal_comma():
    fields, _ = adif.record_to_qso_fields({"tx_pwr": "50 W", "freq": "145,5"})
    assert fields == {"tx_power_w": 50.0, "freq_mhz": 145.5}
