"""Settings › Program, knowledge base, SDR monitor, and the system helpers."""

import io
import json

from shacklog.models import Repeater, StoredFile, SwlEntry
from shacklog.services import markdown_render, system
from shacklog.web.views.sdr import build_memories
from tests.conftest import csrf


# --- system helpers ---------------------------------------------------------

def test_mirror_backup_copies_and_prunes(tmp_path):
    src_dir = tmp_path / "b"
    src_dir.mkdir()
    mirror = tmp_path / "mirror"
    for i in range(5):
        f = src_dir / f"shacklog-2026101{i}-000000-daily.sqlite3"
        f.write_bytes(b"x")
        system.mirror_backup(f, str(mirror), keep=3)
    kept = sorted(p.name for p in mirror.iterdir())
    assert len(kept) == 3 and kept[-1].startswith("shacklog-20261014")
    assert system.mirror_backup(src_dir / "nope", None) is None


def test_server_request_file(tmp_path):
    system.write_server_request(tmp_path, "update")
    assert system.request_file(tmp_path).read_text().strip() == "update"
    try:
        system.write_server_request(tmp_path, "rm -rf")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid action accepted")


def test_checkpoint_folds_wal(db, s):
    from shacklog.models import Equipment

    s.add(Equipment(name="Test radio"))
    s.commit()
    system.checkpoint(db)
    wal = db.path.with_name(db.path.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0


# --- Program page -----------------------------------------------------------

def test_program_page_and_mirror_setting(client, tmp_path, app):
    r = client.get("/settings/program")
    assert r.status_code == 200 and "Your data" in r.text
    assert str(app.config["PATHS"].data_dir) in r.text
    target = tmp_path / "onedrive-backups"
    r = client.post("/settings/program", data={"_csrf": csrf(client), "action": "mirror",
                                                "backup_mirror_dir": str(target),
                                                "backup_mirror_keep": "10"})
    assert r.status_code == 302
    assert any(p.name.endswith("-manual.sqlite3") for p in target.iterdir())


def test_server_controls_need_systemd(tmp_path, monkeypatch):
    from tests.conftest import build_app

    app, _db, paths = build_app(tmp_path / "srv", mode="server")
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["logged_in"] = True
    monkeypatch.setenv("SHACKLOG_CONTROL", "systemd")
    r = c.post("/settings/program", data={"_csrf": csrf(c), "action": "update"})
    assert r.status_code == 200 and "Updating" in r.text
    assert system.request_file(paths.data_dir).read_text().strip() == "update"
    monkeypatch.delenv("SHACKLOG_CONTROL")
    r = c.post("/settings/program", data={"_csrf": csrf(c), "action": "stop"})
    assert r.status_code == 400


# --- knowledge base ---------------------------------------------------------

def test_markdown_is_rendered_safely():
    html = markdown_render.render("# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
                                  "<script>alert(1)</script> [x](javascript:alert(1)) "
                                  "[ok](https://example.org)")
    assert "<table>" in html and "<h1" in html
    assert "<script>" not in html and "javascript:" not in html
    assert 'target="_blank"' in html


def test_kb_note_lifecycle(client, s):
    r = client.post("/kb/new-note", data={"_csrf": csrf(client), "title": "Repeater shifts",
                                          "body": "Italy 70 cm: **+1.6 MHz**", "tags": "how-to"})
    assert r.status_code == 302
    note = s.query(StoredFile).filter_by(category="note").one()
    page = client.get(f"/kb/{note.id}").text
    assert "<strong>+1.6 MHz</strong>" in page
    client.post(f"/kb/{note.id}/pin", data={"_csrf": csrf(client), "pin": "1"})
    assert "Pinned" in client.get("/kb/").text
    assert "Repeater shifts" in client.get("/").text  # dashboard pins
    r = client.post(f"/kb/{note.id}/edit-note", data={"_csrf": csrf(client), "title": "Shifts",
                                                       "body": "# Shifts\n\n2 m: -0.6 MHz"})
    assert r.status_code == 302
    assert "-0.6 MHz" in client.get(f"/kb/{note.id}").text
    assert "Shifts" in client.get("/kb/?q=shifts").text


def test_kb_groups_uploaded_manual(client, s):
    data = {"_csrf": csrf(client), "category": "manual",
            "files": [(io.BytesIO(b"%PDF-1.4\n"), "TM-733 manual.pdf")]}
    client.post("/files/upload", data=data, content_type="multipart/form-data")
    page = client.get("/kb/").text
    assert "Manual" in page and "TM-733 manual" in page


# --- SDR monitor ------------------------------------------------------------

def test_build_memories_format():
    backup, listing = build_memories([(145.6125, "FM", "R1"), (145.6125, "FM", "dup"),
                                      (119.0, "AM", "Malpensa TWR"), (100.0, None, "Radio"),
                                      (5000.0, "FM", "too high")])
    assert backup["app"] == "OFFgrid-SDR" and backup["version"] == 1
    assert len(backup["memories"]) == 50
    used = [m for m in backup["memories"] if m]
    assert [m["band"] for m in used] == ["GEN", "GEN", "FM"]
    assert used[0]["freqHz"] == 145612500 and used[0]["mode"] == "NBFM"
    assert used[1]["mode"] == "AM"
    assert backup["state"]["scanSel"] == [0, 1, 2]
    assert [x["label"] for x in listing] == ["R1", "Malpensa TWR", "Radio"]


def test_sdr_page_memories_and_quick_log(client, s, tmp_path, monkeypatch):
    # Pretend OFFgrid-SDR isn't installed, even on a PC where it sits next to ShackLog.
    from shacklog.web.views import sdr
    monkeypatch.setattr(sdr, "_candidates", lambda: [])
    s.add(Repeater(name="Campo dei Fiori", ident="R1", kind="FM", output_mhz=145.6125,
                   input_mhz=145.0125, latitude=45.87, longitude=8.77))
    s.commit()
    page = client.get("/sdr/")
    assert page.status_code == 200 and "R1" in page.text
    mem = json.loads(client.get("/sdr/memories.json").data)
    assert mem["memories"][0]["freqHz"] == 145612500
    r = client.post("/sdr/heard", data={"_csrf": csrf(client), "freq_mhz": "145,6125",
                                        "mode": "FM", "heard": "iu2abc", "category": "amateur"},
                    headers={"HX-Request": "1"})
    assert r.status_code == 200 and "Logged 145.6125" in r.text
    e = s.query(SwlEntry).one()
    assert e.heard == "IU2ABC" and e.category == "repeater" and e.repeater_id
    assert client.get("/sdr/app").status_code == 404  # OFFgrid-SDR not installed here


def test_sdr_serves_configured_offgrid(client, tmp_path):
    page = tmp_path / "index.html"
    page.write_text("<!DOCTYPE html><title>OFFgrid-SDR</title>")
    r = client.post("/settings/program", data={"_csrf": csrf(client), "action": "sdr_path",
                                                "offgrid_sdr_path": str(page)})
    assert r.status_code == 302
    r = client.get("/sdr/app")
    assert r.status_code == 200 and b"OFFgrid-SDR" in r.data
    assert 'allow="usb' in client.get("/sdr/").text
