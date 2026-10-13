"""QSL cards: to send, labels, mark sent, waiting, card arrived with scans."""

import io
import json
from datetime import date, datetime

from shacklog.models import Link, Qso, StoredFile
from shacklog.services import adif, qsl
from tests.conftest import csrf


def _qso(s, call, when, sent="R", **kw):
    q = Qso(call=call, time_on=datetime.fromisoformat(when), band="2m", mode="FM",
            rst_sent="59", qsl_sent=sent, my_call="IU2TST", **kw)
    s.add(q)
    s.commit()
    return q


def test_extras_keep_other_fields():
    q = Qso(call="X", extra_adif=json.dumps({"sig": "POTA"}))
    qsl.mark_sent(q, date(2026, 10, 13), "B")
    assert q.qsl_sent == "Y" and qsl.sent_info(q) == (date(2026, 10, 13), "B")
    assert json.loads(q.extra_adif)["sig"] == "POTA"
    qsl.mark_sent(q, date(2026, 10, 14), "nonsense")
    assert qsl.sent_info(q) == (date(2026, 10, 14), None)


def test_labels_group_by_call_and_manager():
    a = [Qso(call="IK2ABC", time_on=datetime(2026, 1, d), qsl_via=None) for d in range(1, 7)]
    b = Qso(call="IK2ABC", time_on=datetime(2026, 2, 1), qsl_via="ik2mgr")
    labels = qsl.labels_for(a + [b])
    assert [(l.to, l.via, len(l.qsos)) for l in labels] == [
        ("IK2ABC", None, 4), ("IK2ABC", None, 2), ("IK2ABC", "IK2MGR", 1)]


def test_send_flow(client, s):
    one = _qso(s, "IK2ABC", "2026-10-01T10:00")
    two = _qso(s, "F4XYZ", "2026-10-02T11:00", sent="Q", qsl_via="F5MGR")
    _qso(s, "DL1AAA", "2026-10-03T12:00", sent="N")
    page = client.get("/qsl/").text
    assert "To send (2)" in page and "IK2ABC" in page and "F4XYZ" in page and "DL1AAA" not in page
    labels = client.post("/qsl/mark", data={"_csrf": csrf(client), "action": "labels",
                                            "ids": [one.id, two.id]}).text
    assert "qsl-label" in labels and "via <b>F5MGR</b>" in labels and "de IU2TST" in labels
    r = client.post("/qsl/mark", data={"_csrf": csrf(client), "action": "sent", "ids": [one.id, two.id],
                                       "date": "2026-10-13", "route": "B", "back": "/qsl/"})
    assert r.status_code == 302
    s.expire_all()
    assert s.get(Qso, one.id).qsl_sent == "Y"
    record = adif.qso_to_record(s.get(Qso, one.id))
    assert record["qslsdate"] == "20261013" and record["qsl_sent_via"] == "B"
    waiting = client.get("/qsl/?tab=waiting").text
    assert "Waiting for a card (2)" in waiting and "2026-10-13" in waiting and "Bureau" in waiting


def test_card_arrived_with_scans(client, s):
    q = _qso(s, "IK2ABC", "2026-10-01T10:00", sent="Y")
    _qso(s, "IK2ABC", "2026-10-05T10:00", sent="Y", qsl_rcvd="Y")  # already confirmed
    page = client.get("/qsl/?tab=arrived&call=ik2abc").text
    assert page.count('name="ids"') == 1
    client.post("/qsl/mark", data={
        "_csrf": csrf(client), "action": "received", "ids": [q.id], "date": "2026-11-02",
        "route": "B", "front": (io.BytesIO(b"\xff\xd8front"), "card.jpg"),
        "back": (io.BytesIO(b"\xff\xd8back"), "card-back.jpg")}, content_type="multipart/form-data")
    s.expire_all()
    assert s.get(Qso, q.id).qsl_rcvd == "Y"
    assert qsl.received_info(s.get(Qso, q.id)) == (date(2026, 11, 2), "B")
    files = s.query(StoredFile).filter_by(category="qsl").all()
    assert sorted(f.title for f in files) == ["QSL IK2ABC (back)", "QSL IK2ABC (front)"]
    assert s.query(Link).filter_by(role="QSL card").count() == 2
    assert "No QSO with IK2ABC waiting" in client.get("/qsl/?tab=arrived&call=IK2ABC").text


def test_nothing_ticked_and_bad_back_url(client, s):
    r = client.post("/qsl/mark", data={"_csrf": csrf(client), "action": "sent",
                                       "back": "//evil.example/"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/qsl/")
    assert "QSL cards" in client.get("/qsos/").text  # in the side menu
