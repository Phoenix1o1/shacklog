"""Equipment section: inventory CRUD, filters and the "where is it used" detail page."""

from datetime import datetime

from shacklog.models import Equipment, JournalEntry, Qso, RadioConfig, Station, SwlEntry
from tests.conftest import csrf


def _post(client, target, **fields):
    data = {"_csrf": csrf(client), "kind": "radio", "status": "active"}
    data.update(fields)
    return client.post(target, data=data)


def test_crud(client, s):
    r = _post(client, "/equipment/new", name="Quansheng UV-K1", kind="handheld",
              manufacturer="Quansheng", model="UV-K1", firmware="F4HWN v4.2",
              acquired_on="2026-03-14", price="€35", freq_range="18-1300 MHz",
              url="https://example.org/k1", tags="portable, vhf")
    assert r.status_code == 302
    item = s.query(Equipment).one()
    assert item.kind == "handheld" and item.acquired_on.isoformat() == "2026-03-14"
    assert item.tags == "portable, vhf"
    page = client.get(f"/equipment/{item.id}").text
    assert "F4HWN v4.2" in page and "18-1300 MHz" in page and "Save a radio configuration" in page

    r = _post(client, f"/equipment/{item.id}/edit", name="UV-K1 (Andrea)", kind="handheld",
              status="spare")
    assert r.status_code == 302
    s.expire_all()
    assert s.get(Equipment, item.id).status == "spare"

    assert client.post(f"/equipment/{item.id}/delete", data={"_csrf": csrf(client)}).status_code == 302
    s.expire_all()
    assert s.get(Equipment, item.id).deleted_at is not None
    assert client.get(f"/equipment/{item.id}").status_code == 404


def test_validation(client):
    r = _post(client, "/equipment/new", name="", kind="spaceship", acquired_on="yesterday")
    assert r.status_code == 422
    assert "Required" in r.text and "Pick a value from the list" in r.text and "YYYY-MM-DD" in r.text


def test_filters(client, s):
    s.add_all([
        Equipment(name="Kenwood TM-733E", kind="radio", status="active", manufacturer="Kenwood"),
        Equipment(name="Diamond X-50", kind="antenna", status="active", tags="vhf, uhf"),
        Equipment(name="RTL-SDR v4", kind="receiver", status="retired"),
    ])
    s.commit()
    page = client.get("/equipment/?kind=antenna").text
    assert "Diamond X-50" in page and "TM-733E" not in page
    page = client.get("/equipment/?status=retired").text
    assert "RTL-SDR" in page and "Diamond" not in page
    page = client.get("/equipment/?q=kenwood").text
    assert "TM-733E" in page and "RTL-SDR" not in page
    page = client.get("/equipment/?tags=uhf").text
    assert "Diamond" in page and "TM-733E" not in page
    assert client.get("/equipment/?sort=acquired").status_code == 200


def test_view_gathers_usage(client, s):
    radio = Equipment(name="TM-733E", kind="radio")
    ant = Equipment(name="X-50", kind="antenna")
    s.add_all([radio, ant])
    s.flush()
    milano = s.query(Station).filter_by(name="Milano").one()
    milano.radio_id, milano.antenna_id = radio.id, ant.id
    s.add_all([
        Qso(call="IK2ABC", time_on=datetime(2026, 10, 1, 10), radio_id=radio.id, antenna_id=ant.id),
        Qso(call="IK2XYZ", time_on=datetime(2026, 10, 2, 10), radio_id=radio.id),
        SwlEntry(time_utc=datetime(2026, 10, 3, 9), heard="IR2UFD", receiver_id=radio.id),
        JournalEntry(kind="test", time_utc=datetime(2026, 10, 4), title="SWR sweep of the X-50",
                     equipment_id=ant.id),
        RadioConfig(title="TM-733 Milano repeaters", radio_id=radio.id, is_current=True),
    ])
    s.commit()

    page = client.get(f"/equipment/{radio.id}").text
    assert "IK2ABC" in page and "IK2XYZ" in page
    assert f"/qsos/?radio={radio.id}" in page and "2 QSOs as radio" in page
    assert "IR2UFD" in page and f"/swl/?receiver={radio.id}" in page
    assert "TM-733 Milano repeaters" in page and "current" in page
    assert "Milano" in page  # default at this station

    page = client.get(f"/equipment/{ant.id}").text
    assert "SWR sweep of the X-50" in page
    assert f"/qsos/?antenna={ant.id}" in page and "1 QSOs as antenna" in page
    assert "IK2XYZ" not in page


def test_pages_render(client, s):
    for url in ("/equipment/", "/equipment/new", "/equipment/new?kind=antenna"):
        assert client.get(url).status_code == 200, url
    s.add(Equipment(name="Coax RG-58", kind="feedline"))
    s.commit()
    item = s.query(Equipment).one()
    for url in ("/equipment/", f"/equipment/{item.id}", f"/equipment/{item.id}/edit"):
        assert client.get(url).status_code == 200, url
