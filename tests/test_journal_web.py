"""Journal: entries, measurement rows (add/edit/clear), SWR from power, sweeps, filters."""

import io
import sys
import types

import pytest

from shacklog.models import Equipment, JournalEntry, Measurement
from shacklog.web.views import journal as journal_view
from tests.conftest import csrf

COLS = ["freq_mhz", "swr", "power_fwd_w", "power_ref_w", "return_loss_db", "r_ohm", "x_ohm", "note"]


def _grid(*rows, ids=None, blanks=3):
    """Form fields for the measurement grid: rows are dicts of column -> text."""
    rows = list(rows) + [{}] * blanks
    ids = list(ids or []) + [""] * (len(rows) - len(ids or []))
    data = {"m_id": ids}
    for col in COLS:
        data[f"m_{col}"] = [r.get(col, "") for r in rows]
    return data


def _post(client, url="/journal/new", grid=None, **overrides):
    data = {"_csrf": csrf(client), "kind": "test", "time_utc": "2026-10-12T10:00",
            "title": "Dual-band J-pole", "body": "Trimmed 5 mm.\n\nSecond paragraph.",
            "tags": "j-pole, 2m"}
    data.update(grid or _grid())
    data.update(overrides)
    return client.post(url, data=data)


@pytest.fixture
def fake_touchstone(monkeypatch):
    """A Touchstone double: parses anything except text containing BAD."""
    calls = {"shade": []}
    mod = types.ModuleType("shacklog.services.touchstone")

    def parse_touchstone(text):
        if "BAD" in text:
            raise ValueError("no option line")
        return {"freq_hz": [140e6, 150e6], "s11": [0j, 0j], "z0": 50.0}

    def summarize(parsed):
        return {"min_swr": 1.23, "freq_min_mhz": 145.2, "r_ohm": 48.0, "x_ohm": -3.1,
                "bw_swr2_low_mhz": 143.0, "bw_swr2_high_mhz": 147.5, "start_mhz": 140.0,
                "stop_mhz": 150.0, "points": 101}

    def swr_svg(parsed, width=640, height=260, shade=None):
        calls["shade"].append(shade)
        return '<svg class="sweep-svg" id="fake-sweep"></svg>'

    mod.parse_touchstone, mod.summarize, mod.swr_svg = parse_touchstone, summarize, swr_svg
    monkeypatch.setitem(sys.modules, "shacklog.services.touchstone", mod)
    journal_view._SUMMARY_CACHE.clear()
    return calls


def _attach(client, entry_id, data: bytes, name: str, category="sweep"):
    return client.post("/links/attach", content_type="multipart/form-data", data={
        "_csrf": csrf(client), "a_type": "journal", "a_id": entry_id, "category": category,
        "role": "sweep", "files": (io.BytesIO(data), name)})


def test_swr_from_power():
    assert journal_view.swr_from_power(100, 0) == 1.0
    assert journal_view.swr_from_power(100, 11.1) == pytest.approx(2.0, abs=0.01)
    assert journal_view.swr_from_power(10, 10) is None
    assert journal_view.swr_from_power(None, 1) is None


def test_create_with_measurement_rows(client, s):
    grid = _grid({"freq_mhz": "145,000", "swr": "1.3"},
                 {"freq_mhz": "433.500", "power_fwd_w": "10", "power_ref_w": "0.5",
                  "return_loss_db": "-12.5", "note": "after trim"})
    r = _post(client, grid=grid)
    assert r.status_code == 302
    entry = s.query(JournalEntry).one()
    rows = s.query(Measurement).order_by(Measurement.freq_mhz).all()
    assert [(m.freq_mhz, m.swr) for m in rows] == [(145.0, 1.3), (433.5, None)]
    assert rows[1].return_loss_db == 12.5  # NanoVNA's negative S11 dB stored as return loss
    assert all(m.entry_id == entry.id for m in rows)
    page = client.get(f"/journal/{entry.id}").text
    assert "<p>Trimmed 5 mm.</p>" in page  # |textblock
    assert "calc." in page and "1.58" in page  # SWR computed from 10 W / 0.5 W
    listing = client.get("/journal/").text
    assert "Dual-band J-pole" in listing and "1.30" in listing


def test_edit_changes_and_clears_rows(client, s):
    _post(client, grid=_grid({"freq_mhz": "145", "swr": "1.3"}, {"freq_mhz": "146", "swr": "1.8"}))
    entry = s.query(JournalEntry).one()
    first, second = s.query(Measurement).order_by(Measurement.freq_mhz).all()
    form = client.get(f"/journal/{entry.id}/edit").text
    assert form.count('name="m_freq_mhz"') == 5  # 2 existing + 3 blank
    grid = _grid({"freq_mhz": "145", "swr": "1.2"}, {}, {"freq_mhz": "147", "swr": "2.5"},
                 ids=[first.id, second.id])
    r = _post(client, url=f"/journal/{entry.id}/edit", grid=grid)
    assert r.status_code == 302
    s.expire_all()
    assert s.get(Measurement, first.id).swr == 1.2
    assert s.get(Measurement, second.id).deleted_at is not None  # cleared row: soft delete
    live = s.query(Measurement).filter(Measurement.deleted_at.is_(None)).order_by(Measurement.freq_mhz).all()
    assert [(m.freq_mhz, m.swr) for m in live] == [(145.0, 1.2), (147.0, 2.5)]


def test_invalid_rows_and_fields_return_422(client, s):
    r = _post(client, grid=_grid({"freq_mhz": "abc", "swr": "0.5"}), title="")
    assert r.status_code == 422
    assert "MHz, e.g. 145.500" in r.text and "SWR is 1 or more" in r.text and "Required" in r.text
    assert 'value="abc"' in r.text  # what was typed is shown again
    assert s.query(JournalEntry).count() == 0 and s.query(Measurement).count() == 0


def test_blank_rows_are_ignored(client, s):
    _post(client, kind="diary", title="New coax", grid=_grid())
    assert s.query(JournalEntry).one().kind == "diary"
    assert s.query(Measurement).count() == 0


def test_sweep_is_summarised_and_plotted(client, s, fake_touchstone):
    _post(client)
    entry = s.query(JournalEntry).one()
    page = client.get(f"/journal/{entry.id}").text
    assert "Attach a NanoVNA sweep (.s1p)" in page and 'name="category" value="sweep"' in page
    assert _attach(client, entry.id, b"# Hz S RI R 50\n140000000 0 0\n", "jpole.s1p").status_code == 302
    page = client.get(f"/journal/{entry.id}").text
    assert 'id="fake-sweep"' in page  # SVG inserted as markup
    assert "145.200 MHz" in page and "1.23" in page and "4500 kHz" in page
    assert "48.0 − j3.1 Ω" in page
    assert fake_touchstone["shade"][-1] == [(144.0, 146.0)]  # 70 cm is outside 140–150 MHz
    assert "1.23" in client.get("/journal/").text  # min SWR from the sweep in the list


def test_unparsable_sweep_is_reported(client, s, fake_touchstone):
    _post(client)
    entry = s.query(JournalEntry).one()
    _attach(client, entry.id, b"BAD DATA", "broken.s2p", category="document")
    page = client.get(f"/journal/{entry.id}").text
    assert page.count("Couldn't read this sweep") == 1 and "no option line" in page


def test_sweep_missing_blob_and_missing_module(client, s, filestore, monkeypatch):
    _post(client)
    entry = s.query(JournalEntry).one()
    _attach(client, entry.id, b"# Hz S RI R 50\n", "a.s1p")
    monkeypatch.setattr(journal_view, "_touchstone", lambda: None)
    assert "Sweep plots aren't available" in client.get(f"/journal/{entry.id}").text
    monkeypatch.setattr(type(filestore), "exists", lambda self, sha: False)
    assert "lives on another instance" not in client.get(f"/journal/{entry.id}").text
    assert "bytes live on another instance" in client.get(f"/journal/{entry.id}").text


def test_filters(client, s, fake_touchstone):
    ant = Equipment(name="J-pole", kind="antenna")
    s.add(ant)
    s.commit()
    _post(client, equipment_id=ant.id)
    _post(client, kind="diary", title="Moved the shack", body="Desk by the window")
    swept = s.query(JournalEntry).filter_by(kind="test").one()
    _attach(client, swept.id, b"# Hz S RI R 50\n", "x.s1p")
    client.get("/")
    assert "Moved the shack" not in client.get("/journal/?kind=test").text
    assert "Moved the shack" in client.get("/journal/?q=window").text
    by_eq = client.get(f"/journal/?equipment={ant.id}").text
    assert "Dual-band J-pole" in by_eq and "Moved the shack" not in by_eq
    with_sweep = client.get("/journal/?sweep=yes").text
    assert "Dual-band J-pole" in with_sweep and "Moved the shack" not in with_sweep
    without = client.get("/journal/?sweep=no").text
    assert "Moved the shack" in without and "Dual-band J-pole" not in without
    assert "Dual-band J-pole" in client.get("/journal/?tags=j-pole").text
    assert "No journal entries match" in client.get("/journal/?from=2030-01-01").text


def test_delete_is_soft(client, s):
    _post(client)
    entry = s.query(JournalEntry).one()
    assert client.post(f"/journal/{entry.id}/delete", data={"_csrf": csrf(client)}).status_code == 302
    s.expire_all()
    assert s.get(JournalEntry, entry.id).deleted_at is not None
    assert client.get(f"/journal/{entry.id}").status_code == 404


def test_pages_on_empty_db(client):
    for url in ("/journal/", "/journal/new", "/journal/new?kind=test", "/journal/?sweep=yes"):
        assert client.get(url).status_code == 200
