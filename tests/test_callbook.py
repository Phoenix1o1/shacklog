"""Callbook lookups: HamQTH and QRZ.com answers, sessions, cache (no network)."""

import io
import urllib.parse

import pytest

from shacklog.services import callbook as cb

HAMQTH_LOGIN = b"""<?xml version="1.0"?><HamQTH version="2.8" xmlns="https://www.hamqth.com">
<session><session_id>abc123</session_id></session></HamQTH>"""
HAMQTH_BAD_LOGIN = b"""<?xml version="1.0"?><HamQTH version="2.8" xmlns="https://www.hamqth.com">
<session><error>Wrong user name or password</error></session></HamQTH>"""
HAMQTH_FOUND = """<?xml version="1.0"?><HamQTH version="2.8" xmlns="https://www.hamqth.com">
<search><callsign>ik2abc</callsign><nick>Marco</nick><qth>Varese</qth><country>Italy</country>
<adif>248</adif><grid>JN45NU</grid><adr_name>Marco Rossi</adr_name><adr_street1>Via Roma 1</adr_street1>
<adr_city>Varese</adr_city><adr_zip>21100</adr_zip><adr_country>Italy</adr_country>
<email>m@example.org</email><qsl_via>IK2MGR</qsl_via><latitude>45.82</latitude><longitude>8.83</longitude>
</search></HamQTH>""".encode()
HAMQTH_NOT_FOUND = b"""<?xml version="1.0"?><HamQTH version="2.8" xmlns="https://www.hamqth.com">
<session><error>Callsign not found</error></session></HamQTH>"""
HAMQTH_EXPIRED = b"""<?xml version="1.0"?><HamQTH version="2.8" xmlns="https://www.hamqth.com">
<session><error>Session does not exist or expired</error></session></HamQTH>"""
QRZ_LOGIN = b"""<?xml version="1.0"?><QRZDatabase version="1.34" xmlns="http://xmldata.qrz.com">
<Session><Key>k1</Key><Count>12</Count><SubExp>Wed Jan 1 2027</SubExp></Session></QRZDatabase>"""
QRZ_FOUND = b"""<?xml version="1.0"?><QRZDatabase version="1.34" xmlns="http://xmldata.qrz.com">
<Callsign><call>F4XYZ</call><fname>Jean</fname><name>Dupont</name><addr1>1 rue de la Paix</addr1>
<addr2>Grenoble</addr2><zip>38000</zip><country>France</country><grid>JN25VE</grid>
<lat>45.18</lat><lon>5.72</lon><qslmgr></qslmgr></Callsign>
<Session><Key>k2</Key></Session></QRZDatabase>"""
QRZ_NOT_FOUND = b"""<?xml version="1.0"?><QRZDatabase version="1.34" xmlns="http://xmldata.qrz.com">
<Session><Error>Not found: ZZ9ZZZ</Error><Key>k1</Key></Session></QRZDatabase>"""


class FakeNet:
    def __init__(self, *answers):
        self.answers, self.urls = list(answers), []

    def __call__(self, request, timeout):
        self.urls.append(request.full_url)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(answer)


@pytest.fixture(autouse=True)
def no_sessions():
    cb._sessions.clear()


def test_hamqth_lookup_and_cache(tmp_path):
    net = FakeNet(HAMQTH_LOGIN, HAMQTH_FOUND)
    book = cb.Callbook(tmp_path, "hamqth", "me", "secret", opener=net)
    entry = book.lookup("ik2 abc")
    assert (entry.call, entry.name, entry.qth, entry.locator) == ("IK2ABC", "Marco Rossi", "Varese", "JN45NU")
    assert entry.address == ["Marco Rossi", "Via Roma 1", "21100 Varese", "Italy"]
    assert entry.qsl_via == "IK2MGR" and entry.latitude == 45.82 and entry.source == "HamQTH"
    query = urllib.parse.parse_qs(urllib.parse.urlparse(net.urls[1]).query)
    assert query["id"] == ["abc123"] and query["callsign"] == ["IK2ABC"]
    # from the cache now: no more network
    assert book.lookup("IK2ABC").name == "Marco Rossi" and len(net.urls) == 2
    assert book.cached("ik2abc").qth == "Varese"
    assert book.cache.stats()[0] == 1
    assert entry.form_values() == {"name": "Marco Rossi", "qth": "Varese", "locator": "JN45NU",
                                   "qsl_via": "IK2MGR"}


def test_not_found_is_remembered_for_a_day(tmp_path):
    net = FakeNet(HAMQTH_LOGIN, HAMQTH_NOT_FOUND)
    book = cb.Callbook(tmp_path, "hamqth", "me", "secret", opener=net)
    assert book.lookup("ZZ9ZZZ") is None and book.lookup("ZZ9ZZZ") is None
    assert len(net.urls) == 2
    known, _ = book.cache.get("ZZ9ZZZ", max_age_s=-1)
    assert not known  # expired answers are asked again


def test_expired_session_is_renewed(tmp_path):
    net = FakeNet(HAMQTH_LOGIN, HAMQTH_EXPIRED, HAMQTH_LOGIN, HAMQTH_FOUND)
    assert cb.Callbook(tmp_path, "hamqth", "me", "pw", opener=net).lookup("IK2ABC").name
    assert len(net.urls) == 4


def test_errors(tmp_path):
    with pytest.raises(cb.CallbookError, match="Wrong user"):
        cb.Callbook(tmp_path, "hamqth", "me", "bad", opener=FakeNet(HAMQTH_BAD_LOGIN)).test()
    with pytest.raises(cb.CallbookError, match="can't be reached"):
        cb.Callbook(tmp_path, "hamqth", "me", "pw", opener=FakeNet(OSError("offline"))).lookup("IK2ABC")
    with pytest.raises(cb.CallbookError, match="No callbook"):
        cb.Callbook(tmp_path, "", "", "").lookup("IK2ABC")
    with pytest.raises(cb.CallbookError, match="isn't XML"):
        cb.Callbook(tmp_path, "qrz", "me", "pw", opener=FakeNet(b"<html>")).test()


def test_qrz(tmp_path):
    net = FakeNet(QRZ_LOGIN, QRZ_FOUND, QRZ_NOT_FOUND)
    book = cb.Callbook(tmp_path, "qrz", "me", "pw", opener=net)
    entry = book.lookup("F4XYZ")
    assert (entry.name, entry.qth, entry.locator, entry.source) == ("Jean Dupont", "Grenoble", "JN25VE", "QRZ.com")
    assert entry.address == ["Jean Dupont", "1 rue de la Paix", "38000 Grenoble", "France"]
    assert cb._sessions[("qrz", "me")][0] == "k2"  # QRZ hands out a new key: keep it
    assert book.lookup("ZZ9ZZZ") is None
    assert "agent=ShackLog" in net.urls[0]


def test_password_never_in_error_messages(tmp_path):
    import urllib.error

    err = urllib.error.HTTPError("https://x/?p=topsecret", 500, "Server Error", {}, None)
    with pytest.raises(cb.CallbookError) as caught:
        cb.Callbook(tmp_path, "hamqth", "me", "topsecret", opener=FakeNet(err)).test()
    assert "topsecret" not in str(caught.value)


# ---------------------------------------------------------------------------
# In the web app
# ---------------------------------------------------------------------------

def _setup(client, monkeypatch, *answers):
    from tests.conftest import csrf

    net = FakeNet(*answers)
    real = cb.Callbook.__init__

    def with_fake_net(self, *args, **kwargs):
        real(self, *args, **kwargs)
        self._open = net
    monkeypatch.setattr(cb.Callbook, "__init__", with_fake_net)
    client.post("/settings/", data={"_csrf": csrf(client), "action": "callbook",
                                    "callbook_service": "hamqth", "callbook_user": "me",
                                    "callbook_password": "secret"})
    return net


def test_settings_keep_the_password_and_never_show_it(client, monkeypatch):
    from tests.conftest import csrf

    _setup(client, monkeypatch, HAMQTH_LOGIN)
    page = client.get("/settings/").text
    assert 'value="me"' in page and "secret" not in page and "saved: leave empty" in page
    client.post("/settings/", data={"_csrf": csrf(client), "action": "callbook",
                                    "callbook_service": "hamqth", "callbook_user": "me2",
                                    "callbook_password": ""})
    client.post("/settings/", data={"_csrf": csrf(client), "action": "callbook_test"})
    assert "Logged in to HamQTH as me2" in client.get("/settings/").text


def test_hint_lookup_and_fill(client, monkeypatch, s):
    net = _setup(client, monkeypatch, HAMQTH_LOGIN, HAMQTH_FOUND)
    hint = client.get("/qsos/check-call?call=IK2ABC").text
    assert "Look up IK2ABC in HamQTH" in hint and not net.urls  # typing costs nothing
    box = client.get("/qsos/callbook?call=IK2ABC").text
    assert "Marco Rossi" in box and "data-fill" in box and '"locator": "JN45NU"' in box
    assert "Marco Rossi" in client.get("/qsos/check-call?call=IK2ABC").text  # cached now
    page = client.get("/contacts/call/IK2ABC").text
    assert "Via Roma 1" in page and "From HamQTH" in page


def test_direct_labels_carry_the_cached_address(client, monkeypatch, s):
    from datetime import datetime

    from shacklog.models import Qso
    from tests.conftest import csrf

    _setup(client, monkeypatch, HAMQTH_LOGIN, HAMQTH_FOUND)
    client.get("/qsos/callbook?call=IK2ABC")
    q = Qso(call="IK2ABC", time_on=datetime(2026, 10, 1, 10, 0), qsl_sent="R")
    s.add(q)
    s.commit()
    sheet = client.post("/qsl/mark", data={"_csrf": csrf(client), "action": "labels",
                                           "ids": [q.id], "route": "D"}).text
    assert "Via Roma 1" in sheet and "21100 Varese" in sheet
    bureau = client.post("/qsl/mark", data={"_csrf": csrf(client), "action": "labels",
                                            "ids": [q.id], "route": "B"}).text
    assert "Via Roma 1" not in bureau
