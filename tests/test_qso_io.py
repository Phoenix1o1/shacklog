"""ADIF import/export against the database (services/qso_io.py)."""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from shacklog.models import Qso, Station
from shacklog.services import adif, qso_io

DATA = Path(__file__).parent / "data" / "formats"


def _stations(s):
    return {st.name: st for st in s.query(Station).all()}


def _rec(call, when: datetime, freq="145.500", mode="FM", **extra):
    rec = {"call": call, "qso_date": when.strftime("%Y%m%d"), "time_on": when.strftime("%H%M%S"),
           "freq": freq, "mode": mode}
    rec.update(extra)
    return rec


def test_import_sample_file(s):
    data = (DATA / "sample.adi").read_bytes()
    summary = qso_io.import_adif(s, data, filename="sample.adi")
    s.commit()
    assert (summary.created, summary.duplicates, summary.skipped) == (4, 1, 2)
    assert "Record 4: no CALL" in summary.errors
    assert any(e.startswith("Record 5 (IZ2NOT)") and "TIME_ON" in e for e in summary.errors)
    assert any("TX_PWR" in e for e in summary.errors)  # kept, but reported
    assert len(summary.created_ids) == 4
    st = _stations(s)

    ik = s.query(Qso).filter_by(call="IK2ABC").one()
    assert ik.station_id == st["Milano"].id  # STATION_CALLSIGN + JN45 square
    assert ik.import_ref == "sample.adi" and ik.qth == "Città Studi"
    assert json.loads(ik.extra_adif) == {"app_testlog_rig": "TM-733", "distance": "3.2"}
    assert ik.distance_km is not None and ik.distance_km < 20  # enrich ran
    assert ik.my_latitude is not None

    f1 = s.query(Qso).filter_by(call="F1ZCQ").one()
    assert f1.station_id == st["Grenoble"].id and f1.my_call == "F/IU2TST"
    assert abs(f1.latitude - 45.149) < 1e-6  # file coordinates beat the locator centre
    assert f1.band == "2m"  # derived from FREQ

    dl = s.query(Qso).filter_by(call="DL1XYZ").one()
    assert dl.station_id == st["Milano"].id  # 4-character MY_GRIDSQUARE matches JN45ol
    assert dl.band == "70cm" and (dl.mode, dl.submode) == ("DIGITALVOICE", "DMR")
    assert dl.time_off == datetime(2026, 10, 12, 0, 10)
    assert json.loads(dl.extra_adif) == {"tx_pwr": "abc"}

    sat = s.query(Qso).filter_by(call="ON4SAT").one()
    assert sat.station_id is None and sat.extra_adif is None  # no STATION_CALLSIGN
    assert (sat.sat_name, sat.sat_mode, sat.prop_mode) == ("ISS", "V/U", "SAT")


def test_file_values_survive_enrichment(s):
    st = _stations(s)
    rec = _rec("IK2ABC", datetime(2026, 10, 1, 10, 0), station_callsign="IU2TST/P",
               my_gridsquare="JN45ab", operator="IU2OPR")
    qso_io.import_adif(s, adif.write_adi([rec]), station_id=st["Milano"].id)
    q = s.query(Qso).one()
    assert q.station_id == st["Milano"].id
    assert (q.my_call, q.my_locator, q.operator) == ("IU2TST/P", "JN45ab", "IU2OPR")


def test_station_matching_needs_same_square(s):
    rec = _rec("IK2ABC", datetime(2026, 10, 1, 10, 0), station_callsign="IU2TST",
               my_gridsquare="JN33aa")
    qso_io.import_adif(s, adif.write_adi([rec]))
    assert s.query(Qso).one().station_id is None


def test_explicit_station_and_station_snapshot(s):
    st = _stations(s)
    qso_io.import_adif(s, adif.write_adi([_rec("IK2ABC", datetime(2026, 10, 1, 10, 0))]),
                       station_id=st["Grenoble"].id)
    q = s.query(Qso).one()
    assert q.station_id == st["Grenoble"].id
    assert (q.my_call, q.my_locator) == ("F/IU2TST", "JN25vd")  # filled by enrich


def test_duplicates_against_log_and_within_file(s):
    t0 = datetime(2026, 10, 1, 10, 0)
    first = qso_io.import_adif(s, adif.write_adi([_rec("IK2ABC", t0)]))
    s.commit()
    assert first.created == 1
    records = [
        _rec("IK2ABC", t0 + timedelta(seconds=110)),          # same QSO, within 2 minutes
        _rec("IK2ABC", t0 + timedelta(minutes=3)),            # new QSO
        _rec("IK2ABC", t0 + timedelta(minutes=4)),            # dup of the previous line
        _rec("IK2ABC", t0, freq="433.500"),                   # other band: new
        _rec("IK2ABC", t0, mode="SSB"),                       # other mode: new
        _rec("IK2ABC", t0, mode="DMR", freq="438.6"),         # DMR alias
        _rec("IK2ABC", t0, mode="DIGITALVOICE", submode="DMR", freq="438.6"),  # same as alias
    ]
    summary = qso_io.import_adif(s, adif.write_adi(records))
    assert (summary.created, summary.duplicates) == (4, 3)


def test_deleted_qso_is_not_a_duplicate(s):
    t0 = datetime(2026, 10, 1, 10, 0)
    qso_io.import_adif(s, adif.write_adi([_rec("IK2ABC", t0)]))
    s.query(Qso).one().soft_delete()
    s.commit()
    assert qso_io.import_adif(s, adif.write_adi([_rec("IK2ABC", t0)])).created == 1


def test_export_then_reimport_is_all_duplicates_and_lossless(s):
    qso_io.import_adif(s, (DATA / "sample.adi").read_bytes(), filename="sample.adi")
    s.commit()
    qsos = s.query(Qso).order_by(Qso.time_on).all()
    text = qso_io.export_adif(qsos)
    _, records = adif.parse_adi(text)
    assert [r["app_shacklog_id"] for r in records] == [q.id for q in qsos]
    ik = next(r for r in records if r["call"] == "IK2ABC")
    assert ik["app_testlog_rig"] == "TM-733" and ik["distance"] == "3.2"
    assert ik["qth"] == "Città Studi" and ik["station_callsign"] == "IU2TST"

    again = qso_io.import_adif(s, text)
    assert (again.created, again.duplicates) == (0, 4)

    # Even with the time moved (edited elsewhere), the ShackLog id recognises the QSO.
    moved = [dict(r, time_on="000000") for r in records]
    assert qso_io.import_adif(s, adif.write_adi(moved)).duplicates == 4


def test_tags_round_trip(s):
    rec = _rec("IK2ABC", datetime(2026, 10, 1, 10, 0), app_shacklog_tags="sota, portable")
    qso_io.import_adif(s, adif.write_adi([rec]))
    q = s.query(Qso).one()
    assert q.tags == "sota, portable"
    out = adif.parse_adi(qso_io.export_adif([q]))[1][0]
    assert out["app_shacklog_tags"] == "sota, portable"


def test_empty_or_garbage_file(s):
    summary = qso_io.import_adif(s, b"this is not adif")
    assert summary.created == 0 and summary.errors
    payload = json.loads(qso_io.summary_json(summary))
    assert set(payload) == {"created", "duplicates", "skipped", "errors", "created_ids"}


def test_import_does_not_commit(s, db):
    qso_io.import_adif(s, adif.write_adi([_rec("IK2ABC", datetime(2026, 10, 1, 10, 0))]))
    s.rollback()
    assert s.query(Qso).count() == 0


def test_ten_thousand_records_are_fast(s):
    t0 = datetime(2025, 1, 1)
    records = [_rec(f"IK{i % 900:03d}X", t0 + timedelta(minutes=7 * i), station_callsign="IU2TST",
                    my_gridsquare="JN45ol", gridsquare="JN45nm") for i in range(10_000)]
    records += records[:500]  # some duplicates inside the file
    data = adif.write_adi(records).encode()
    start = time.perf_counter()
    summary = qso_io.import_adif(s, data, filename="big.adi")
    s.commit()
    elapsed = time.perf_counter() - start
    assert (summary.created, summary.duplicates) == (10_000, 500)
    # Generous: catches quadratic slow-downs (minutes), not slow disks (OneDrive, CI).
    assert elapsed < 120, f"import took {elapsed:.1f}s"
    # A second pass is all duplicates and also quick.
    start = time.perf_counter()
    again = qso_io.import_adif(s, data)
    assert again.created == 0 and again.duplicates == 10_500
    assert time.perf_counter() - start < 5
