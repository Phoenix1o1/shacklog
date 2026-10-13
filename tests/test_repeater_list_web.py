"""Importing a repeater list through the web page: preview, mapping, import, duplicates."""

import io
import re
from pathlib import Path

from shacklog.models import ImportLog, Repeater
from tests.conftest import csrf

DATA = Path(__file__).parent / "data"


def _upload(client, name):
    return client.post("/repeaters/import-list", data={
        "_csrf": csrf(client), "file": (io.BytesIO((DATA / name).read_bytes()), name)},
        content_type="multipart/form-data")


def _confirm(client, page, **changes):
    token = re.search(r'name="token" value="([0-9a-f]+)"', page).group(1)
    form = {"_csrf": csrf(client), "token": token, "country": "Italy"}
    for index, field in re.findall(r'<select id="col_(\d+)" name="col_\d+">(.*?)</select>', page, re.S):
        chosen = re.search(r'<option value="([^"]*)" selected>', field)
        form[f"col_{index}"] = chosen.group(1) if chosen else ""
    form.update(changes)
    return client.post("/repeaters/import-list", data=form)


def test_preview_then_import_then_duplicates(client, s):
    assert "Import a repeater list" in client.get("/repeaters/").text
    page = _upload(client, "ponti_it.csv").text
    assert "ponti_it.csv" in page and "3 with an output frequency" in page
    assert "IR2UFD" in page and "Import 3 repeaters" in page
    done = _confirm(client, page)
    assert done.status_code == 200 and "Import finished" in done.text
    reps = {r.callsign: r for r in s.query(Repeater)}
    assert set(reps) == {"IR2UFD", "IR2UGM", "IR1UGW"}
    ufd = reps["IR2UFD"]
    assert ufd.country == "Italy" and ufd.region == "Lombardia" and ufd.latitude is not None
    assert ufd.source.startswith("CSV ponti_it.csv")
    assert s.query(ImportLog).filter_by(kind="repeater-list").count() == 1
    # The same list again: nothing new.
    again = _confirm(client, _upload(client, "ponti_it.csv").text)
    assert "repeaters added" in again.text and s.query(Repeater).count() == 3


def test_mapping_can_be_corrected(client, s):
    page = _upload(client, "repeaterbook.json").text
    # Say the "Landmark" column is the location instead of "Nearest City".
    headers = re.findall(r'<label for="col_(\d+)">([^<]*)</label>', page)
    landmark = next(i for i, h in headers if h == "Landmark")
    city = next(i for i, h in headers if h == "Nearest City")
    _confirm(client, page, **{f"col_{landmark}": "location_name", f"col_{city}": ""})
    ufd = s.query(Repeater).filter_by(callsign="IR2UFD").one()
    assert ufd.location_name == "Campo dei Fiori"


def test_bad_file_and_expired_upload(client):
    r = client.post("/repeaters/import-list", data={
        "_csrf": csrf(client), "file": (io.BytesIO(b"nothing useful"), "x.csv")},
        content_type="multipart/form-data")
    assert r.status_code == 302
    r = client.post("/repeaters/import-list", data={"_csrf": csrf(client), "token": "0" * 32})
    assert r.status_code == 302
    assert client.post("/repeaters/import-list",
                       data={"_csrf": csrf(client), "token": "../../etc"}).status_code == 400
