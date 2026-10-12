"""Repeaters section: form, filters (near/band/call), detail page, CHIRP import and export."""

import csv
import io
from datetime import datetime

import pytest

from shacklog.models import (
    ImportLog,
    Qso,
    RadioChannel,
    RadioConfig,
    Repeater,
    Station,
    SwlEntry,
    utcnow,
)
from shacklog.services import chirp, geo
from tests.conftest import csrf

#: A CHIRP export as a radio would produce it: four repeaters, one simplex calling
#: channel, one receive-only beacon. Positions come from locators in the comments.
CHIRP_CSV = (
    "Location,Name,Frequency,Duplex,Offset,Tone,rToneFreq,cToneFreq,DtcsCode,DtcsPolarity,"
    "RxDtcsCode,CrossMode,Mode,TStep,Skip,Power,Comment,URCALL,RPT1CALL,RPT2CALL,DVCODE\r\n"
    "1,F1ZAA,145.600000,-,0.600000,Tone,88.5,88.5,023,NN,023,Tone->Tone,FM,12.50,,5.0W,Grenoble JN25vd,,,,\r\n"
    "2,F5ZBB,430.050000,+,9.400000,Tone,67.0,88.5,023,NN,023,Tone->Tone,FM,12.50,,5.0W,Vercors JN25ra,,,,\r\n"
    "3,IR2UFD,145.762500,-,0.600000,Tone,123.0,88.5,023,NN,023,Tone->Tone,FM,12.50,,5.0W,Milano JN45ol,,,,\r\n"
    "4,CALLING,145.500000,,0.000000,,88.5,88.5,023,NN,023,Tone->Tone,FM,12.50,,5.0W,,,,,\r\n"
    "5,DMR GRE,439.000000,-,7.600000,,88.5,88.5,023,NN,023,Tone->Tone,DMR,12.50,,5.0W,JN25ue,,,,\r\n"
    "6,BEACON,144.400000,off,0.000000,,88.5,88.5,023,NN,023,Tone->Tone,FM,12.50,,5.0W,,,,,\r\n"
)


def _post(client, target, **fields):
    data = {"_csrf": csrf(client), "kind": "FM", "status": "active"}
    data.update(fields)
    return client.post(target, data=data)


def _import(client, text=CHIRP_CSV, **options):
    data = {"_csrf": csrf(client), "file": (io.BytesIO(text.encode()), "grenoble.csv")}
    data.update(options)
    return client.post("/repeaters/import", data=data, content_type="multipart/form-data")


def _by_name(s, name):
    return s.query(Repeater).filter_by(name=name, deleted_at=None).one()


# ---------------------------------------------------------------------------
# Form
# ---------------------------------------------------------------------------

def test_create_applies_shift_and_position(client, s):
    r = _post(client, "/repeaters/new", name="Grenoble R6", callsign="f1zaa", ident="R6",
              output_mhz="145,6", shift="-0.6", ctcss_up="88.5", locator="jn25vd",
              country="France", talkgroups="", last_verified="2026-10-01")
    assert r.status_code == 302
    rep = _by_name(s, "Grenoble R6")
    assert rep.callsign == "F1ZAA" and rep.output_mhz == 145.6
    assert rep.input_mhz == 145.0 and rep.offset_mhz == -0.6
    assert rep.locator == "JN25vd" and rep.latitude is not None
    page = client.get(f"/repeaters/{rep.id}").text
    assert "-0.600" in page and "88.5 Hz" in page


def test_input_wins_over_shift_and_coordinates_fill_locator(client, s):
    _post(client, "/repeaters/new", name="Penice", output_mhz="430.3", input_mhz="431.9",
          shift="-5", latitude="44.80", longitude="9.31")
    rep = _by_name(s, "Penice")
    assert rep.input_mhz == 431.9 and rep.offset_mhz == pytest.approx(1.6)
    assert rep.locator == geo.latlon_to_locator(44.80, 9.31, 6)


def test_validation(client):
    r = _post(client, "/repeaters/new", name="", shift="-0.6", locator="nowhere", kind="FOO")
    assert r.status_code == 422
    for message in ("Required", "Give the output frequency", "Not a Maidenhead locator",
                    "Pick a value from the list"):
        assert message in r.text


def test_edit_and_soft_delete(client, s):
    _post(client, "/repeaters/new", name="R1", output_mhz="145.6125", input_mhz="145.0125")
    rep = _by_name(s, "R1")
    r = _post(client, f"/repeaters/{rep.id}/edit", name="R1 Campo dei Fiori",
              output_mhz="145.6125", input_mhz="145.0125", status="inactive", tags="linked")
    assert r.status_code == 302
    s.expire_all()
    assert s.get(Repeater, rep.id).status == "inactive"
    assert client.post(f"/repeaters/{rep.id}/delete").status_code == 400  # no CSRF
    assert client.post(f"/repeaters/{rep.id}/delete", data={"_csrf": csrf(client)}).status_code == 302
    s.expire_all()
    assert s.get(Repeater, rep.id).deleted_at is not None
    assert client.get(f"/repeaters/{rep.id}").status_code == 404


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def test_filters_near_band_call(client, s):
    _import(client)
    client.get("/")  # consume flashes
    near = client.get("/repeaters/?near=Grenoble&radius=60").text
    assert "F1ZAA" in near and "F5ZBB" in near and "IR2UFD" not in near
    assert "km from Grenoble" in near
    near_home = client.get("/repeaters/?near=JN45ol&radius=20").text
    assert "IR2UFD" in near_home and "F1ZAA" not in near_home
    seventy = client.get("/repeaters/?band=70cm").text
    assert "F5ZBB" in seventy and "F1ZAA" not in seventy
    by_call = client.get("/repeaters/?call=F1*").text
    assert "F1ZAA" in by_call and "F5ZBB" not in by_call
    by_kind = client.get("/repeaters/?kind=DMR").text
    assert "DMR GRE" in by_kind and "F1ZAA" not in by_kind
    by_freq = client.get("/repeaters/?fmin=145.7&fmax=146").text
    assert "IR2UFD" in by_freq and "F1ZAA" not in by_freq
    with_tone = client.get("/repeaters/?ctcss=yes").text
    assert "F1ZAA" in with_tone and "DMR GRE" not in with_tone
    assert "DMR GRE" in client.get("/repeaters/?ctcss=no").text
    # Default list measures from the reference station (Milano): IR2UFD comes first.
    full = client.get("/repeaters/").text
    assert "km from Milano" in full and full.index("IR2UFD") < full.index("F1ZAA")


def test_bad_filter_value_is_reported(client):
    page = client.get("/repeaters/?near=atlantis&radius=10")
    assert page.status_code == 200 and "Couldn" in page.text


# ---------------------------------------------------------------------------
# CHIRP import → export
# ---------------------------------------------------------------------------

def test_chirp_import_creates_repeaters_and_skips_duplicates(client, s):
    r = _import(client, country="France", region="Isère", source="Andrea's K1")
    assert r.status_code == 200
    assert "4</b><span>repeaters added" in r.text.replace("\n", "")
    reps = {x.name: x for x in s.query(Repeater).all()}
    assert set(reps) == {"F1ZAA", "F5ZBB", "IR2UFD", "DMR GRE"}  # CALLING and BEACON skipped
    f1 = reps["F1ZAA"]
    assert f1.callsign == "F1ZAA" and f1.input_mhz == 145.0 and f1.ctcss_up == 88.5
    assert f1.locator == "JN25vd" and f1.latitude is not None
    assert f1.country == "France" and f1.region == "Isère" and f1.source == "Andrea's K1"
    assert reps["F5ZBB"].offset_mhz == pytest.approx(9.4)
    assert reps["DMR GRE"].kind == "DMR" and reps["DMR GRE"].callsign is None
    assert s.query(ImportLog).filter_by(kind="chirp-repeaters").count() == 1

    again = _import(client)
    assert "0</b><span>repeaters added" in again.text.replace("\n", "")
    assert "4</b><span>already here" in again.text.replace("\n", "")
    s.expire_all()
    assert s.query(Repeater).count() == 4


def test_chirp_import_include_simplex(client, s):
    _import(client, include_simplex="1")
    names = {x.name for x in s.query(Repeater).all()}
    assert {"CALLING", "BEACON"} <= names
    beacon = _by_name(s, "BEACON")
    assert beacon.input_mhz is None  # receive only


def test_chirp_import_rejects_missing_file(client):
    r = client.post("/repeaters/import", data={"_csrf": csrf(client)},
                    content_type="multipart/form-data")
    assert r.status_code == 302


def _export_rows(response):
    return list(csv.DictReader(io.StringIO(response.text)))


def test_chirp_export_of_filtered_set_by_distance(client, s):
    _import(client)
    # Channels within 60 km of Grenoble for the UV-K1, nearest first, from memory 10.
    r = client.get("/repeaters/export-chirp.csv?near=Grenoble&radius=60&order=distance"
                   "&start=10&name_len=4&opts=1&fm_only=1")
    assert r.status_code == 200
    disposition = r.headers["Content-Disposition"]
    assert f"{utcnow():%Y%m%d}" in disposition and "grenoble-60km" in disposition
    rows = _export_rows(r)
    assert [row["Name"] for row in rows] == ["F1ZA", "F5ZB"]  # DMR excluded, Milano too far
    assert [row["Location"] for row in rows] == ["10", "11"]
    assert rows[0]["Duplex"] == "-" and float(rows[0]["Offset"]) == pytest.approx(0.6)
    assert rows[0]["Tone"] == "Tone" and float(rows[0]["rToneFreq"]) == 88.5

    # Without "analog only" the DMR repeater (≈7 km) sits between the two.
    rows = _export_rows(client.get("/repeaters/export-chirp.csv?near=Grenoble&radius=60"
                                   "&order=distance&opts=1"))
    assert [row["Name"] for row in rows] == ["F1ZAA", "DMR GRE", "F5ZBB"]

    # By frequency, at most two channels.
    rows = _export_rows(client.get("/repeaters/export-chirp.csv?near=Grenoble&radius=60"
                                   "&order=freq&max=2&opts=1"))
    assert [float(row["Frequency"]) for row in rows] == [145.6, 430.05]

    # The exported file imports back into CHIRP-shaped channels.
    channels = chirp.read_chirp_csv(client.get("/repeaters/export-chirp.csv").text)
    assert {c["freq_mhz"] for c in channels} == {145.6, 430.05, 145.7625}


def test_export_form_carries_filters(client):
    _import(client)
    page = client.get("/repeaters/?near=Grenoble&radius=60&sort=distance").text
    assert 'name="near" value="Grenoble"' in page and 'name="radius" value="60"' in page
    assert "Download CHIRP CSV" in page and "Distance from Grenoble" in page


def test_plain_csv_export(client, s):
    _import(client)
    r = client.get("/repeaters/export.csv?band=2m")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert {row["name"] for row in rows} == {"F1ZAA", "IR2UFD"}
    f1 = next(row for row in rows if row["name"] == "F1ZAA")
    assert f1["locator"] == "JN25vd" and float(f1["offset_mhz"]) == pytest.approx(-0.6)
    assert "id" in rows[0] and "last_verified" in rows[0]


# ---------------------------------------------------------------------------
# Detail page
# ---------------------------------------------------------------------------

def test_detail_page_gathers_everything(client, s):
    _import(client)
    rep = _by_name(s, "F1ZAA")
    grenoble = s.query(Station).filter_by(name="Grenoble").one()
    cfg = RadioConfig(title="UV-K1 FieldOps", is_current=True)
    s.add(cfg)
    s.flush()
    s.add_all([
        Qso(call="F4ABC", time_on=datetime(2026, 10, 5, 18, 30), repeater_id=rep.id,
            station_id=grenoble.id, mode="FM"),
        SwlEntry(time_utc=datetime(2026, 10, 6, 7), heard="F1ZAA", repeater_id=rep.id),
        RadioChannel(config_id=cfg.id, location=12, name="GRE R6", freq_mhz=145.6,
                     repeater_id=rep.id),
    ])
    s.commit()
    page = client.get(f"/repeaters/{rep.id}").text
    assert "F4ABC" in page and f"/qsos/?repeater={rep.id}" in page
    assert f"/swl/?repeater={rep.id}" in page
    assert "UV-K1 FieldOps" in page and "memory 12" in page
    assert f"/qsos/new?repeater={rep.id}" in page and "Log QSO via this repeater" in page
    assert "From my stations" in page and "Milano" in page and "Grenoble" in page
    assert "-0.600" in page


def test_pages_render_empty_and_full(client, s):
    urls = ["/repeaters/", "/repeaters/new", "/repeaters/import", "/repeaters/export.csv",
            "/repeaters/export-chirp.csv", "/repeaters/?sort=name&dir=desc",
            "/repeaters/?sort=country", "/repeaters/?sort=output"]
    for url in urls:
        assert client.get(url).status_code == 200, url
    _import(client)
    for url in urls:
        assert client.get(url).status_code == 200, url
    for rep in s.query(Repeater).all():
        assert client.get(f"/repeaters/{rep.id}").status_code == 200
        assert client.get(f"/repeaters/{rep.id}/edit").status_code == 200
