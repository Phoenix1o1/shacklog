"""Library: upload (multiple, duplicates, link to equipment), preview, safe raw/download
headers, metadata edit, filters, soft delete, fetching a missing blob from the hub."""

import io
import sys
import types

import pytest

from shacklog.db import set_setting
from shacklog.models import Equipment, Link, StoredFile
from tests.conftest import csrf


def _upload(client, files, **fields):
    data = {"_csrf": csrf(client), "category": "", "tags": "", "description": "",
            "equipment_id": ""}
    data.update(fields)
    data["files"] = [(io.BytesIO(content), name) for name, content in files]
    return client.post("/files/upload", data=data, content_type="multipart/form-data")


def _file(s, filename) -> StoredFile:
    s.expire_all()
    return s.query(StoredFile).filter_by(filename=filename).one()


def test_upload_several_files_and_link_to_equipment(client, s):
    radio = Equipment(name="TM-733E", kind="radio")
    s.add(radio)
    s.commit()
    r = _upload(client, [("TM-733 manual.txt", b"Kenwood TM-733 instruction manual\nCTCSS"),
                         ("front.png", b"\x89PNG\r\n\x1a\nfake"),
                         ("ant.s1p", b"# Hz S RI R 50\n")],
                tags="tm-733", description="From the box", equipment_id=radio.id)
    assert r.status_code == 302 and r.headers["Location"].endswith("/files/")
    assert s.query(StoredFile).count() == 3
    manual = _file(s, "TM-733 manual.txt")
    assert manual.category == "document" and manual.tags == "tm-733"
    assert manual.title == "TM-733 manual" and "instruction manual" in manual.text_content
    assert _file(s, "front.png").category == "photo"
    assert _file(s, "ant.s1p").category == "sweep"  # guessed from the extension
    assert s.query(Link).filter_by(a_type="equipment", a_id=radio.id).count() == 3
    page = client.get(f"/files/{manual.id}").text
    assert "TM-733E" in page  # "Used by" panel
    assert "Indexed" in page and manual.sha256 in page


def test_duplicate_upload_reuses_entry(client, s):
    _upload(client, [("manual.pdf", b"%PDF-1.4 same bytes")], category="manual")
    first = _file(s, "manual.pdf")
    r = _upload(client, [("copy of manual.pdf", b"%PDF-1.4 same bytes")], category="manual")
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/files/{first.id}")
    assert s.query(StoredFile).count() == 1
    page = client.get(r.headers["Location"]).text
    assert "already in the library" in page and "kept the existing entry" in page


def test_upload_needs_a_file(client, s):
    r = _upload(client, [])
    assert r.status_code == 422 and "Choose at least one file" in r.text
    r = _upload(client, [("a.txt", b"x")], equipment_id="no-such-id")
    assert r.status_code == 422 and s.query(StoredFile).count() == 0


@pytest.mark.parametrize("name, content, inline, ctype", [
    ("photo.png", b"\x89PNG\r\n\x1a\n", True, "image/png"),
    ("pic.jpg", b"\xff\xd8\xff\xe0", True, "image/jpeg"),
    ("manual.pdf", b"%PDF-1.4\n", True, "application/pdf"),
    ("notes.txt", "Ciao, è un test".encode("latin-1"), True, "text/plain; charset=iso-8859-1"),
    ("page.html", b"<script>alert(1)</script>", False, "application/octet-stream"),
    ("logo.svg", b"<svg onload='alert(1)'/>", False, "application/octet-stream"),
    ("firmware.bin", b"\x00\x01", False, "application/octet-stream"),
])
def test_raw_headers_are_safe(client, s, name, content, inline, ctype):
    _upload(client, [(name, content)])
    f = _file(s, name)
    r = client.get(f"/files/{f.id}/raw")
    assert r.status_code == 200 and r.data == content
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    # PDFs are exempt: Chrome blocks PDFs under a sandbox CSP.
    if ctype == "application/pdf":
        assert "Content-Security-Policy" not in r.headers
    else:
        assert r.headers["Content-Security-Policy"] == "sandbox"
    assert r.headers["Content-Type"] == ctype
    disposition = r.headers.get("Content-Disposition", "")
    assert disposition.startswith("inline") if inline else disposition.startswith("attachment")


def test_download_is_attachment_with_original_name(client, s):
    _upload(client, [("TM 733 manual.pdf", b"%PDF-1.4\n"), ("x.html", b"<b>hi</b>")])
    f = _file(s, "TM 733 manual.pdf")
    r = client.get(f"/files/{f.id}/download")
    assert r.status_code == 200 and r.data == b"%PDF-1.4\n"
    assert r.headers["Content-Disposition"].startswith("attachment")
    assert "TM 733 manual.pdf" in r.headers["Content-Disposition"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    html = _file(s, "x.html")
    r = client.get(f"/files/{html.id}/download")
    assert r.headers["Content-Type"] == "application/octet-stream"


def test_previews(client, s):
    lines = "\n".join(f"line {i}" for i in range(400)).encode()
    _upload(client, [("long.log", lines), ("p.webp", b"RIFF0000WEBP"), ("doc.pdf", b"%PDF-1.4"),
                     ("evil.html", b"<script>alert(1)</script>"), ("blob.zip", b"PK\x03\x04")])
    page = client.get(f"/files/{_file(s, 'long.log').id}").text
    assert "line 299" in page and "line 300" not in page and "First 300 lines" in page
    assert '<img src="/files/' in client.get(f"/files/{_file(s, 'p.webp').id}").text
    assert "<iframe" in client.get(f"/files/{_file(s, 'doc.pdf').id}").text
    evil = client.get(f"/files/{_file(s, 'evil.html').id}").text
    assert "&lt;script&gt;alert(1)" in evil and "<script>alert(1)" not in evil
    assert "No preview" in client.get(f"/files/{_file(s, 'blob.zip').id}").text


def test_edit_metadata(client, s):
    _upload(client, [("a.txt", b"abc")])
    f = _file(s, "a.txt")
    form = client.get(f"/files/{f.id}/edit")
    assert form.status_code == 200
    r = client.post(f"/files/{f.id}/edit", data={"_csrf": csrf(client), "title": "",
                                                 "category": "manual"})
    assert r.status_code == 422
    r = client.post(f"/files/{f.id}/edit", data={
        "_csrf": csrf(client), "title": "Antenna notes", "category": "datasheet",
        "tags": "vna, j-pole", "description": "Measured"})
    assert r.status_code == 302
    f = _file(s, "a.txt")
    assert (f.title, f.category, f.tags) == ("Antenna notes", "datasheet", "vna, j-pole")


def test_filters_and_sorts(client, s):
    radio = Equipment(name="UV-K5", kind="handheld")
    s.add(radio)
    s.commit()
    _upload(client, [("k5 manual.txt", b"Quansheng UV-K5 manual text")], category="manual",
            equipment_id=radio.id, tags="uv-k5")
    _upload(client, [("photo.png", b"\x89PNG\r\n\x1a\nzz")])
    client.get("/")
    page = client.get("/files/?category=manual").text
    assert "k5 manual" in page and "photo.png" not in page
    assert "k5 manual" not in client.get("/files/?q=photo").text
    assert "k5 manual" in client.get("/files/?text=yes").text
    assert "k5 manual" not in client.get("/files/?text=no").text
    assert "k5 manual" in client.get("/files/?linked=equipment").text
    unlinked = client.get("/files/?linked=none").text
    assert "photo.png" in unlinked and "k5 manual" not in unlinked
    assert "k5 manual" in client.get("/files/?tags=uv-k5").text
    assert "Couldn&#39;t understand" in client.get("/files/?linked=bogus").text
    for sort in ("new", "name", "size"):
        assert client.get(f"/files/?sort={sort}").status_code == 200


def test_delete_is_soft(client, s, filestore):
    _upload(client, [("a.txt", b"keep the bytes")])
    f = _file(s, "a.txt")
    assert client.post(f"/files/{f.id}/delete", data={"_csrf": csrf(client)}).status_code == 302
    f = _file(s, "a.txt")
    assert f.deleted_at is not None and filestore.exists(f.sha256)
    assert client.get(f"/files/{f.id}").status_code == 404


def test_missing_blob_offers_fetch_from_hub(client, s, db, filestore, monkeypatch):
    f = StoredFile(sha256="ab" * 32, filename="remote.pdf", mime="application/pdf", size=10,
                   title="Remote manual")
    s.add(f)
    s.commit()
    page = client.get(f"/files/{f.id}").text
    assert "stored on another ShackLog instance" in page and "Settings › Sync" in page
    assert client.get(f"/files/{f.id}/raw").status_code == 404
    assert client.get(f"/files/{f.id}/download").status_code == 302
    with db.transaction() as tx:
        set_setting(tx, "sync_hub_url", "http://hub.example:8573")
    assert "Fetch from hub" in client.get(f"/files/{f.id}").text

    fetched = []
    mod = types.ModuleType("shacklog.services.sync_client")

    def fetch_blob_from_hub(database, store, sha256):
        fetched.append(sha256)
        store.put_bytes(b"0123456789")  # pretend: in reality the hub sends these bytes
        return False

    mod.fetch_blob_from_hub = fetch_blob_from_hub
    monkeypatch.setitem(sys.modules, "shacklog.services.sync_client", mod)
    r = client.post(f"/files/{f.id}/fetch", data={"_csrf": csrf(client)})
    assert r.status_code == 302 and fetched == [f.sha256]
    assert "have this file either" in client.get(r.headers["Location"]).text
    mod.fetch_blob_from_hub = lambda database, store, sha256: True
    r = client.post(f"/files/{f.id}/fetch", data={"_csrf": csrf(client)})
    assert "Fetched" in client.get(r.headers["Location"]).text


def test_pages_on_empty_db(client):
    for url in ("/files/", "/files/upload", "/files/?linked=none&text=yes&sort=size"):
        assert client.get(url).status_code == 200
    assert client.get("/files/new").status_code == 302
    assert client.get("/files/nope").status_code == 404
