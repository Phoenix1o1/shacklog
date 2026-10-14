"""Radio configurations: CHIRP snapshot upload, channel parsing, repeater matching, diff, search."""

import csv
import io

from shacklog.models import Equipment, Link, RadioChannel, RadioConfig, Repeater, StoredFile
from shacklog.services import chirp
from shacklog.web.views import radio
from tests.conftest import csrf

HEADER = ("Location,Name,Frequency,Duplex,Offset,Tone,rToneFreq,cToneFreq,DtcsCode,"
          "DtcsPolarity,RxDtcsCode,CrossMode,Mode,TStep,Skip,Power,Comment,URCALL,RPT1CALL,"
          "RPT2CALL,DVCODE,Bank\r\n")

V1 = HEADER + (
    "1,MI R1,145.612500,-,0.600000,Tone,88.5,88.5,023,NN,023,Tone->Tone,FM,12.50,,High,,,,,,Fusion\r\n"
    "2,MI RU5,430.300000,+,1.600000,Tone,67.0,88.5,023,NN,023,Tone->Tone,FM,12.50,,High,,,,,,Fusion\r\n"
    "3,CALL,145.500000,,0.000000,,88.5,88.5,023,NN,023,Tone->Tone,FM,12.50,,Low,,,,,,Fusion\r\n"
)
V2 = HEADER + (
    "1,MI R1,145.612500,-,0.600000,TSQL,88.5,88.5,023,NN,023,Tone->Tone,FM,12.50,,High,,,,,,Fusion\r\n"
    "3,CALL,145.500000,,0.000000,,88.5,88.5,023,NN,023,Tone->Tone,FM,12.50,,Low,,,,,,Fusion\r\n"
    "4,ISS,145.800000,,0.000000,,88.5,88.5,023,NN,023,Tone->Tone,FM,12.50,S,Low,,,,,,Labs\r\n"
)


def _radio(s, name="UV-K1"):
    item = Equipment(name=name, kind="handheld", firmware="F4HWN v4.2")
    s.add(item)
    s.commit()
    return item


def _repeaters(s):
    # Two repeaters on 145.6125: only one has the -0.6 shift the channel uses.
    right = Repeater(name="Milano R1", callsign="IR2UFD", output_mhz=145.6125, input_mhz=145.0125)
    wrong = Repeater(name="Odd split", output_mhz=145.6125, input_mhz=145.6125)
    ru5 = Repeater(name="Milano RU5", callsign="IR2UFE", output_mhz=430.3, input_mhz=431.9)
    s.add_all([wrong, right, ru5])
    s.commit()
    return right, wrong, ru5


def _new_config(client, radio_id, title, csv_text=None, img=None, **fields):
    data = {"_csrf": csrf(client), "radio_id": radio_id, "title": title,
            "config_date": "2026-10-12", "edition": "Fusion", "is_current": "1"}
    data.update(fields)
    if csv_text is not None:
        data["csv_file"] = (io.BytesIO(csv_text.encode()), "k1.csv")
    if img is not None:
        data["image_file"] = (io.BytesIO(img), "k1.img")
    return client.post("/radio/new", data=data, content_type="multipart/form-data")


def _config(s, title):
    return s.query(RadioConfig).filter_by(title=title).one()


def test_create_with_csv_parses_and_matches(client, s):
    item = _radio(s)
    right, wrong, ru5 = _repeaters(s)
    r = _new_config(client, item.id, "K1 Fusion v1", V1, img=b"\x00CHIRPIMG" * 64,
                    settings="Squelch 3, dual watch off")
    assert r.status_code == 302
    cfg = _config(s, "K1 Fusion v1")
    assert cfg.is_current and cfg.radio_id == item.id
    channels = s.query(RadioChannel).filter_by(config_id=cfg.id).order_by(RadioChannel.location).all()
    assert [c.location for c in channels] == [1, 2, 3]
    assert channels[0].repeater_id == right.id  # shift decided between the two 145.6125
    assert channels[1].repeater_id == ru5.id
    assert channels[2].repeater_id is None
    assert '"Bank": "Fusion"' in channels[0].raw  # unknown column preserved

    # Files went to the library (category chirp) and are linked with their roles.
    image, csv_file = s.get(StoredFile, cfg.image_file_id), s.get(StoredFile, cfg.csv_file_id)
    assert image.category == "chirp" and csv_file.category == "chirp"
    roles = {l.role for l in s.query(Link).filter_by(a_id=cfg.id)}
    assert roles == {"chirp image", "chirp csv"}

    page = client.get(f"/radio/{cfg.id}").text
    assert "MI RU5" in page and "IR2UFD" in page and "+1.600" in page and "T 88.5" in page
    assert f"/files/{image.id}/download" in page and f"/files/{csv_file.id}/download" in page
    assert "Squelch 3" in page


def test_current_flag_is_exclusive_per_radio(client, s):
    k1, tm = _radio(s, "UV-K1"), _radio(s, "TM-733E")
    _new_config(client, k1.id, "K1 old")
    _new_config(client, tm.id, "TM current")
    _new_config(client, k1.id, "K1 new")
    client.get("/")  # consume the "Saved …" flash messages
    s.expire_all()
    assert not _config(s, "K1 old").is_current
    assert _config(s, "K1 new").is_current and _config(s, "TM current").is_current
    page = client.get("/radio/?current=yes").text
    assert "K1 new" in page and "TM current" in page and "K1 old" not in page


def test_edit_with_new_csv_replaces_channels(client, s):
    item = _radio(s)
    _new_config(client, item.id, "K1", V1)
    cfg = _config(s, "K1")
    data = {"_csrf": csrf(client), "radio_id": item.id, "title": "K1", "is_current": "1",
            "csv_file": (io.BytesIO(V2.encode()), "k1-v2.csv")}
    r = client.post(f"/radio/{cfg.id}/edit", data=data, content_type="multipart/form-data")
    assert r.status_code == 302
    s.expire_all()
    live = s.query(RadioChannel).filter_by(config_id=cfg.id, deleted_at=None).all()
    assert sorted(c.location for c in live) == [1, 3, 4]
    assert s.query(RadioChannel).filter(RadioChannel.config_id == cfg.id,
                                        RadioChannel.deleted_at.isnot(None)).count() == 3


def test_validation(client, s):
    r = _new_config(client, "", "", config_date="12/10/2026")
    assert r.status_code == 422 and "Required" in r.text and "YYYY-MM-DD" in r.text
    r = _new_config(client, "", "Empty", "Location,Name,Frequency\r\n")
    assert r.status_code == 422 and "no channels" in r.text
    assert s.query(RadioConfig).count() == 0


def test_diff(client, s):
    item = _radio(s)
    _new_config(client, item.id, "v1", V1)
    _new_config(client, item.id, "v2", V2)
    a, b = _config(s, "v1"), _config(s, "v2")
    r = client.get(f"/radio/diff?a={a.id}&b={b.id}")
    assert r.status_code == 200
    text = r.text.replace("\n", "")
    assert "1</b><span>added" in text and "1</b><span>removed" in text and "1</b><span>changed" in text
    assert "ISS" in r.text and "MI RU5" in r.text and "tone_mode" in r.text
    same = client.get(f"/radio/diff?a={a.id}&b={a.id}").text
    assert "The two channel lists are the same" in same
    assert "/radio/diff" in client.get(f"/radio/{b.id}").text  # "Compare with…" form


def test_channel_search(client, s):
    item = _radio(s)
    _new_config(client, item.id, "v1", V1)
    _new_config(client, item.id, "v2", V2)
    page = client.get("/radio/channels?q=145.6125").text
    assert page.count("MI R1") == 2 and "CALL" not in page
    assert "MI R1" in client.get("/radio/channels?q=145612.5").text  # kHz
    assert "CALL" in client.get("/radio/channels?q=145500").text
    by_prefix = client.get("/radio/channels?q=145.8").text
    assert "ISS" in by_prefix and "MI R1" not in by_prefix
    by_name = client.get("/radio/channels?q=ru5").text
    assert "MI RU5" in by_name and "ISS" not in by_name
    current = client.get("/radio/channels?q=MI&current=yes").text
    assert "v2" in current and "MI RU5" not in current
    assert "programmed in any saved configuration" in client.get("/radio/channels?q=433.5").text


def test_export_config_as_chirp(client, s):
    item = _radio(s)
    _new_config(client, item.id, "K1 Fusion", V1)
    cfg = _config(s, "K1 Fusion")
    r = client.get(f"/radio/{cfg.id}/export.csv")
    assert r.status_code == 200 and "k1-fusion" in r.headers["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [row["Name"] for row in rows] == ["MI R1", "MI RU5", "CALL"]
    assert rows[0]["Bank"] == "Fusion"  # extra column round-trips
    assert chirp.diff_channels(chirp.read_chirp_csv(V1), chirp.read_chirp_csv(r.text)) == {
        "added": [], "removed": [], "changed": []}


def test_rematch_after_new_repeaters(client, s):
    item = _radio(s)
    _new_config(client, item.id, "K1", V1)
    cfg = _config(s, "K1")
    assert s.query(RadioChannel).filter(RadioChannel.repeater_id.isnot(None)).count() == 0
    _repeaters(s)
    assert client.post(f"/radio/{cfg.id}/rematch", data={"_csrf": csrf(client)}).status_code == 302
    s.expire_all()
    assert s.query(RadioChannel).filter(RadioChannel.config_id == cfg.id,
                                        RadioChannel.repeater_id.isnot(None)).count() == 2


def test_soft_delete_hides_channels(client, s):
    item = _radio(s)
    _new_config(client, item.id, "K1", V1)
    cfg = _config(s, "K1")
    assert client.post(f"/radio/{cfg.id}/delete", data={"_csrf": csrf(client)}).status_code == 302
    s.expire_all()
    assert s.get(RadioConfig, cfg.id).deleted_at is not None
    assert client.get(f"/radio/{cfg.id}").status_code == 404
    assert "MI R1" not in client.get("/radio/channels?q=145.6125").text


def test_match_repeater_helper():
    a = Repeater(name="a", output_mhz=145.6, input_mhz=145.0)
    b = Repeater(name="b", output_mhz=145.6005, input_mhz=145.6005)
    assert radio.match_repeater(145.6, -0.6, [b, a]) is a
    assert radio.match_repeater(145.6, 0.0, [a, b]) is b
    assert radio.match_repeater(145.6, None, [a, b]) is a  # nearest output
    assert radio.match_repeater(145.61, -0.6, [a, b]) is None
    assert radio.channel_shift("-", 0.6, 145.6) == -0.6
    assert radio.channel_shift("split", 431.9, 430.3) == 1.6
    assert radio.channel_shift("off", 0, 144.4) is None


def test_pages_render(client, s):
    for url in ("/radio/", "/radio/new", "/radio/channels", "/radio/?sort=date",
                "/radio/?sort=title&dir=desc"):
        assert client.get(url).status_code == 200, url
    item = _radio(s)
    assert client.get(f"/radio/new?radio={item.id}").status_code == 200
    _new_config(client, item.id, "K1", V1)
    _new_config(client, item.id, "No CSV")
    for cfg in s.query(RadioConfig).all():
        for url in (f"/radio/{cfg.id}", f"/radio/{cfg.id}/edit", f"/radio/new?from={cfg.id}",
                    f"/radio/{cfg.id}/export.csv"):
            assert client.get(url).status_code == 200, url
    for url in ("/radio/", "/radio/?sort=radio", f"/radio/?radio={item.id}", "/radio/?edition=Fusion"):
        assert client.get(url).status_code == 200, url
