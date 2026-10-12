"""Contacts: cards (unique callsign), callsigns in the log, and the callsign page."""

from datetime import datetime

from shacklog.models import Contact, Qso, Repeater, Station, SwlEntry
from tests.conftest import csrf


def _post(client, target, **fields):
    data = {"_csrf": csrf(client)}
    data.update(fields)
    return client.post(target, data=data)


def _log(s):
    milano = s.query(Station).filter_by(name="Milano").one()
    grenoble = s.query(Station).filter_by(name="Grenoble").one()
    rep = Repeater(name="Grenoble R6", callsign="F1ZAA", output_mhz=145.6, input_mhz=145.0)
    s.add(rep)
    s.flush()
    s.add_all([
        Qso(call="IK2ABC", time_on=datetime(2025, 5, 1, 9), band="2m", mode="FM", name="Marco",
            qth="Milano", station_id=milano.id, distance_km=12.0),
        Qso(call="IK2ABC", time_on=datetime(2026, 9, 1, 9), band="70cm", mode="FM",
            station_id=milano.id, distance_km=3.5),
        Qso(call="IK2ABC", time_on=datetime(2026, 10, 1, 9), band="2m", mode="DIGITALVOICE",
            submode="DMR", name="Marco", qth="Sesto", station_id=grenoble.id,
            repeater_id=rep.id, distance_km=275.0),
        Qso(call="F/IK2ABC/P", time_on=datetime(2026, 8, 15, 12), band="2m", mode="FM"),
        Qso(call="F1ZAA", time_on=datetime(2026, 10, 2, 9), band="2m", mode="FM"),
        SwlEntry(time_utc=datetime(2026, 10, 3, 7), heard="ik2abc", freq_mhz=145.6),
        SwlEntry(time_utc=datetime(2026, 10, 4, 7), heard="IK2XYZ", freq_mhz=145.6),
    ])
    s.commit()
    return rep


def test_card_crud_and_unique_callsign(client, s):
    r = _post(client, "/contacts/new", callsign="ik2 abc", name="Marco", locator="jn45nm",
              tags="club")
    assert r.status_code == 302 and r.headers["Location"].endswith("/contacts/call/IK2ABC")
    card = s.query(Contact).one()
    assert card.callsign == "IK2ABC" and card.latitude is not None

    dup = _post(client, "/contacts/new", callsign="IK2ABC")
    assert dup.status_code == 422
    assert "There is already a card for IK2ABC" in dup.text
    assert f'href="/contacts/{card.id}"' in dup.text

    r = _post(client, f"/contacts/{card.id}/edit", callsign="IK2ABC", name="Marco R.")
    assert r.status_code == 302
    s.expire_all()
    assert s.get(Contact, card.id).name == "Marco R."

    assert client.post(f"/contacts/{card.id}/delete", data={"_csrf": csrf(client)}).status_code == 302
    s.expire_all()
    assert s.get(Contact, card.id).deleted_at is not None
    # A deleted card no longer blocks a new one for the same callsign.
    assert _post(client, "/contacts/new", callsign="IK2ABC").status_code == 302


def test_validation(client):
    r = _post(client, "/contacts/new", callsign="", locator="ZZ12", email="x")
    assert r.status_code == 422
    assert "Required" in r.text and "Not a Maidenhead locator" in r.text


def test_new_card_prefilled_from_latest_qso(client, s):
    _log(s)
    form = client.get("/contacts/new?call=ik2abc").text
    assert 'value="IK2ABC"' in form and 'value="Marco"' in form and 'value="Sesto"' in form
    _post(client, "/contacts/new", callsign="IK2ABC", name="Marco")
    card = s.query(Contact).one()
    # Asking for a new card when one exists opens the existing one instead.
    r = client.get("/contacts/new?call=IK2ABC")
    assert r.status_code == 302 and card.id in r.headers["Location"]


def test_index_aggregates_callsigns(client, s):
    _log(s)
    s.add(Contact(callsign="F1ZAA", name="Repeater sysop"))
    s.commit()
    page = client.get("/contacts/").text
    assert "Repeater sysop" in page
    calls = page[page.index("Callsigns in the log"):]
    # Sorted by QSO count: IK2ABC (3) first.
    assert calls.index("IK2ABC") < calls.index("F1ZAA")
    row = calls[calls.index(">IK2ABC<"):calls.index(">IK2ABC<") + 900]
    assert ">3<" in row and "2025-05-01" in row and "2026-10-01" in row
    assert "Marco · Sesto" in row and "2m 70cm" in row
    assert "card" in calls[calls.index(">F1ZAA<"):calls.index(">F1ZAA<") + 200]

    filtered = client.get("/contacts/?call=F*").text
    filtered = filtered[filtered.index("Callsigns in the log"):]
    assert "F1ZAA" in filtered and "F/IK2ABC/P" in filtered and ">IK2ABC<" not in filtered

    by_last = client.get("/contacts/?csort=last&cdir=desc").text
    by_last = by_last[by_last.index("Callsigns in the log"):]
    assert by_last.index(">F1ZAA<") < by_last.index(">IK2ABC<")

    cards = client.get("/contacts/?q=sysop").text
    assert "Repeater sysop" in cards


def test_callsign_page(client, s):
    rep = _log(s)
    page = client.get("/contacts/call/ik2abc")
    assert page.status_code == 302 and page.headers["Location"].endswith("/contacts/call/IK2ABC")
    page = client.get("/contacts/call/IK2ABC").text
    assert "Add a contact card" in page and "/contacts/new?call=IK2ABC" in page
    assert "/qsos/new?call=IK2ABC" in page and "Log QSO with IK2ABC" in page
    assert "2025-05-01" in page and "2026-10-01" in page  # first / last
    assert "2m 70cm" in page and "DMR FM" in page  # bands, modes
    assert "275 km" in page  # farthest
    assert "Grenoble R6" in page  # via
    assert "heard" in page and "2026-10-03" in page  # SWL match is case-insensitive
    assert "2026-10-04" not in page
    assert "F/IK2ABC/P" not in page  # a different callsign

    # The repeater's own callsign lists the repeater.
    rpt_page = client.get("/contacts/call/F1ZAA").text
    assert "Repeaters with this call" in rpt_page and f"/repeaters/{rep.id}" in rpt_page


def test_callsign_with_slashes(client, s):
    _log(s)
    r = client.get("/contacts/call/f/ik2abc/p")
    assert r.status_code == 302 and r.headers["Location"].endswith("/contacts/call/F/IK2ABC/P")
    page = client.get("/contacts/call/F/IK2ABC/P").text
    assert "2026-08-15" in page and "Log QSO with F/IK2ABC/P" in page
    assert client.get("/contacts/call?call=f/ik2abc/p").headers["Location"].endswith(
        "/contacts/call/F/IK2ABC/P")


def test_card_view_is_the_callsign_page(client, s):
    _log(s)
    s.add(Contact(callsign="IK2ABC", name="Marco", notes="Meets on R6 on Sundays"))
    s.commit()
    card = s.query(Contact).one()
    page = client.get(f"/contacts/{card.id}").text
    assert "Meets on R6 on Sundays" in page and "Edit card" in page
    assert "QSL cards, photos &amp; links" in page  # links panel for the card


def test_pages_render(client, s):
    for url in ("/contacts/", "/contacts/new", "/contacts/call/NOBODY", "/contacts/?csort=first",
                "/contacts/?csort=call&cdir=asc", "/contacts/?sort=name"):
        assert client.get(url).status_code == 200, url
    _log(s)
    s.add(Contact(callsign="IK2ABC"))
    s.commit()
    card = s.query(Contact).one()
    for url in ("/contacts/", "/contacts/call/IK2ABC", "/contacts/call/F1ZAA", f"/contacts/{card.id}",
                f"/contacts/{card.id}/edit", "/contacts/?call=IK2*"):
        assert client.get(url).status_code == 200, url
