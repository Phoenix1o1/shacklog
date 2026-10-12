"""CHIRP CSV reading/writing, repeater ↔ channel conversion and channel diff (services/chirp.py)."""

import csv
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shacklog.models import RadioChannel
from shacklog.services import chirp

DATA = Path(__file__).parent / "data" / "formats"


def _rep(**kw):
    base = dict(ident=None, callsign=None, name="", kind="FM", output_mhz=None, input_mhz=None,
                ctcss_up=None, ctcss_down=None, dcs_code=None, location_name=None)
    base.update(kw)
    return SimpleNamespace(**base)


#: Fictional but plausible repeaters around Milan and Grenoble.
R1_MILANO = _rep(ident="R1", callsign="IR2UFD", name="Milano R1 Campo dei Fiori",
                 output_mhz=145.6125, input_mhz=145.0125, ctcss_up=156.7,
                 location_name="Campo dei Fiori")
F1ZCQ = _rep(callsign="F1ZCQ", name="Grenoble Chamrousse", output_mhz=145.650,
             input_mhz=145.050, location_name="Chamrousse")
RU5_70CM = _rep(ident="RU5", callsign="IR2UFE", name="Milano RU5", output_mhz=430.150,
                input_mhz=431.750, ctcss_up=71.9, ctcss_down=71.9)
CROSS_BAND = _rep(ident="RX", name="Cross-band link", output_mhz=430.3125,
                  input_mhz=145.3125, ctcss_up=88.5, ctcss_down=123.0)
DCS_REP = _rep(callsign="IR1DCS", name="Some DCS repeater", output_mhz=439.000,
               input_mhz=431.400, dcs_code="23")


# ---------------------------------------------------------------------------
# read_chirp_csv
# ---------------------------------------------------------------------------

def test_read_sample_file():
    channels = chirp.read_chirp_csv((DATA / "uvk1.csv").read_text(encoding="utf-8"))
    assert [c["location"] for c in channels] == [1, 2, 3, 4, 5]  # empty memory skipped
    r1, zcq, ru5, simplex, iss = channels
    assert set(r1) == set(chirp.CHANNEL_FIELDS) | {"raw"}
    assert all(hasattr(RadioChannel, k) for k in chirp.CHANNEL_FIELDS)
    assert r1["name"] == "R1 MI" and r1["freq_mhz"] == 145.6125
    assert (r1["duplex"], r1["offset_mhz"], r1["tone_mode"], r1["rtone"]) == ("-", 0.6, "Tone", 156.7)
    assert r1["raw"]["Location"] == "1" and r1["raw"]["Bank"] == "Milano"  # extra column kept
    assert zcq["tone_mode"] == "TSQL"
    assert (ru5["freq_mhz"], ru5["duplex"], ru5["offset_mhz"], ru5["rtone"]) == (430.15, "+", 1.6, 71.9)
    assert simplex["duplex"] == "" and simplex["tone_mode"] == "" and simplex["power"] == "1.5W"
    assert iss["duplex"] == "off" and iss["skip"] == "S" and iss["tstep"] == "5.00"


def test_read_tolerates_bom_crlf_missing_columns_and_case():
    text = "﻿location,name,frequency,Duplex,Offset\r\n7,S20,145.500000,,0\r\n,next,145.525,,\r\n"
    channels = chirp.read_chirp_csv(text)
    assert [(c["location"], c["name"], c["freq_mhz"]) for c in channels] == [
        (7, "S20", 145.5), (8, "next", 145.525)]  # missing Location continues the numbering
    assert channels[0]["tone_mode"] == "" and channels[0]["rtone"] is None
    assert channels[0]["raw"] == {"location": "7", "name": "S20", "frequency": "145.500000",
                                  "Duplex": "", "Offset": "0"}


def test_read_semicolon_decimal_comma_and_bytes():
    text = "Location;Name;Frequency;Duplex;Offset;Tone;rToneFreq\n1;R1;145,612500;-;0,600000;Tone;156,7\n"
    for data in (text, text.encode("utf-8")):
        ch = chirp.read_chirp_csv(data)[0]
        assert (ch["freq_mhz"], ch["offset_mhz"], ch["rtone"]) == (145.6125, 0.6, 156.7)


def test_read_empty_input():
    assert chirp.read_chirp_csv("") == []
    assert chirp.read_chirp_csv(",".join(chirp.CHIRP_COLUMNS) + "\n") == []


# ---------------------------------------------------------------------------
# write_chirp_csv
# ---------------------------------------------------------------------------

def _rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def test_write_defaults_and_formatting():
    text = chirp.write_chirp_csv([{"location": 3, "name": "S20", "freq_mhz": 145.5},
                                  {"name": "PMR1", "freq_mhz": 446.00625}])
    assert text.splitlines()[0] == ",".join(chirp.CHIRP_COLUMNS)
    assert "\r\n" in text
    first, second = _rows(text)
    assert first["Frequency"] == "145.500000" and first["Offset"] == "0.000000"
    assert (first["rToneFreq"], first["cToneFreq"], first["DtcsCode"]) == ("88.5", "88.5", "023")
    assert (first["DtcsPolarity"], first["CrossMode"], first["Mode"]) == ("NN", "Tone->Tone", "FM")
    assert first["TStep"] == "12.50" and first["Power"] == chirp.DEFAULT_POWER
    assert second["Location"] == "4"  # numbered after the highest
    assert second["Frequency"] == "446.006250" and second["TStep"] == "6.25"  # off the 12.5 raster


def test_write_keeps_extra_raw_columns_and_dstar_fields():
    ch = {"location": 1, "name": "DV", "freq_mhz": 439.5, "mode": "DV",
          "raw": {"URCALL": "CQCQCQ", "Bank": "Milano"}}
    text = chirp.write_chirp_csv([ch])
    header = text.splitlines()[0].split(",")
    assert header[-1] == "Bank" and header[:-1] == chirp.CHIRP_COLUMNS
    row = _rows(text)[0]
    assert row["URCALL"] == "CQCQCQ" and row["Bank"] == "Milano"


def test_write_accepts_radio_channel_objects_with_json_raw():
    ch = RadioChannel(location=2, name="R1", freq_mhz=145.6125, duplex="-", offset_mhz=-0.6,
                      tone_mode="Tone", rtone=156.7, raw=json.dumps({"Bank": "Milano"}))
    row = _rows(chirp.write_chirp_csv([ch]))[0]
    assert row["Offset"] == "0.600000"  # magnitude; the sign is in Duplex
    assert row["rToneFreq"] == "156.7" and row["Bank"] == "Milano"


def test_round_trip_read_write_read():
    original = (DATA / "uvk1.csv").read_text(encoding="utf-8")
    first = chirp.read_chirp_csv(original)
    written = chirp.write_chirp_csv(first)
    second = chirp.read_chirp_csv(written)
    strip = lambda chans: [{k: v for k, v in c.items() if k != "raw"} for c in chans]  # noqa: E731
    assert strip(second) == strip(first)
    assert [c["raw"]["Bank"] for c in second] == [c["raw"]["Bank"] for c in first]
    assert chirp.write_chirp_csv(second) == written


# ---------------------------------------------------------------------------
# repeater_to_channel / channel_to_repeater_fields
# ---------------------------------------------------------------------------

def test_repeater_minus_shift_with_tone():
    ch = chirp.repeater_to_channel(R1_MILANO, 1)
    assert (ch["location"], ch["name"], ch["freq_mhz"]) == (1, "R1", 145.6125)
    assert (ch["duplex"], ch["offset_mhz"]) == ("-", pytest.approx(0.6))
    assert (ch["tone_mode"], ch["rtone"], ch["mode"]) == ("Tone", 156.7, "FM")
    assert ch["comment"] == "IR2UFD Campo dei Fiori" and ch["tstep"] == "12.50"


def test_repeater_without_tone_and_name_fallbacks():
    ch = chirp.repeater_to_channel(F1ZCQ, 2)
    assert ch["name"] == "F1ZCQ" and ch["tone_mode"] == "" and ch["duplex"] == "-"
    assert ch["comment"] == "Chamrousse"
    named = chirp.repeater_to_channel(_rep(name="A very long repeater name", output_mhz=145.5), 9)
    assert named["name"] == "A very lon" and named["duplex"] == ""  # simplex, 10 chars
    assert chirp.repeater_to_channel(R1_MILANO, 1, name_length=6)["name"] == "R1"


def test_repeater_italian_70cm_tsql():
    ch = chirp.repeater_to_channel(RU5_70CM, 3)
    assert (ch["duplex"], ch["offset_mhz"]) == ("+", pytest.approx(1.6))
    assert (ch["tone_mode"], ch["rtone"], ch["ctone"]) == ("TSQL", 71.9, 71.9)


def test_repeater_cross_band_and_cross_tone():
    ch = chirp.repeater_to_channel(CROSS_BAND, 4)
    assert (ch["duplex"], ch["offset_mhz"]) == ("split", 145.3125)
    assert (ch["tone_mode"], ch["cross_mode"], ch["rtone"], ch["ctone"]) == (
        "Cross", "Tone->Tone", 88.5, 123.0)


def test_repeater_dcs_and_digital_modes():
    ch = chirp.repeater_to_channel(DCS_REP, 5)
    assert (ch["tone_mode"], ch["dtcs"], ch["rx_dtcs"]) == ("DTCS", "023", "023")
    dmr = chirp.repeater_to_channel(_rep(ident="DMR1", kind="DMR", output_mhz=438.6, input_mhz=431.0), 6)
    assert dmr["mode"] == "DMR"
    echo = chirp.repeater_to_channel(_rep(ident="EL", kind="ECHOLINK", output_mhz=145.275), 7)
    assert echo["mode"] == "FM"


def test_iss_receive_only_channel_to_repeater():
    iss = chirp.read_chirp_csv((DATA / "uvk1.csv").read_text(encoding="utf-8"))[4]
    fields = chirp.channel_to_repeater_fields(iss)
    assert fields["output_mhz"] == 145.8 and fields["input_mhz"] is None
    assert fields["notes"] == "ISS downlink RX only" and fields["kind"] == "FM"


@pytest.mark.parametrize("rep", [R1_MILANO, F1ZCQ, RU5_70CM, CROSS_BAND, DCS_REP],
                         ids=["R1", "F1ZCQ", "RU5", "cross", "dcs"])
def test_repeater_channel_round_trip(rep):
    ch = chirp.repeater_to_channel(rep, 1)
    # Through the CSV text too, as when a generated file comes back from CHIRP.
    back = chirp.read_chirp_csv(chirp.write_chirp_csv([ch]))[0]
    fields = chirp.channel_to_repeater_fields(back)
    assert fields["output_mhz"] == pytest.approx(rep.output_mhz)
    assert fields["input_mhz"] == pytest.approx(rep.input_mhz)
    assert fields["ctcss_up"] == rep.ctcss_up
    assert fields["ctcss_down"] == rep.ctcss_down
    assert fields["dcs_code"] == (f"{int(rep.dcs_code):03d}" if rep.dcs_code else None)
    assert fields["kind"] == "FM"


# ---------------------------------------------------------------------------
# diff_channels
# ---------------------------------------------------------------------------

def test_diff_channels():
    old = chirp.read_chirp_csv((DATA / "uvk1.csv").read_text(encoding="utf-8"))
    new = [dict(c) for c in old if c["location"] != 4]
    new[0] = dict(new[0], rtone=123.0, raw={"different": "x"})  # raw ignored
    new[1] = dict(new[1], name="ZCQ", comment="moved")
    new.append(dict(old[3], location=10))
    diff = chirp.diff_channels(old, new)
    assert [c["location"] for c in diff["added"]] == [10]
    assert [c["location"] for c in diff["removed"]] == [4]
    assert [(a["location"], fields) for a, _b, fields in diff["changed"]] == [
        (1, ["rtone"]), (2, ["name", "comment"])]


def test_diff_ignores_float_noise_and_none_vs_empty():
    a = [{"location": 1, "freq_mhz": 145.6125, "comment": None, "raw": {}}]
    b = [{"location": 1, "freq_mhz": 145.61250000001, "comment": "", "raw": {"x": 1}}]
    a[0]["tstep"], b[0]["tstep"] = "12.5", "12.50"
    assert chirp.diff_channels(a, b) == {"added": [], "removed": [], "changed": []}
