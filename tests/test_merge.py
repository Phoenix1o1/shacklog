"""Duplicate finder and merge."""

from datetime import datetime

from shacklog.db import get_setting, set_setting
from shacklog.models import Link, Qso, Station, StoredFile
from shacklog.services import merge
from shacklog.services.links import add_link
from tests.conftest import csrf


def _dup_station(s):
    home = s.query(Station).filter_by(name="Milano").one()
    twin = Station(name="milano ", kind="home", locator="JN45ol", callsign="IU2TST",
                   altitude_m=120, notes=None)
    s.add(twin)
    s.flush()
    return home, twin


def test_finder_and_merge_moves_everything(s):
    home, twin = _dup_station(s)
    q = Qso(call="IK2ABC", time_on=datetime(2026, 10, 1), station_id=twin.id)
    f = StoredFile(sha256="0" * 64, filename="pic.jpg")
    s.add_all([q, f])
    s.flush()
    add_link(s, twin, f, role="photo")
    add_link(s, home, f, role="photo")  # becomes a duplicate link after the merge
    set_setting(s, "default_station_id", twin.id)
    s.commit()

    groups = merge.find_duplicates(s)
    assert [g.entity for g in groups] == ["station"] and len(groups[0].records) == 2

    result = merge.merge(s, Station, home.id, [twin.id])
    s.commit()
    assert result.merged == 1 and result.references >= 1
    assert s.get(Qso, q.id).station_id == home.id
    assert s.get(Station, twin.id).deleted_at is not None
    assert get_setting(s, "default_station_id") == home.id
    assert s.get(Station, home.id).altitude_m == 120  # empty field filled
    live_links = (s.query(Link).filter(Link.deleted_at.is_(None))
                      .filter(Link.role != merge.MERGED_ROLE).all())
    assert len(live_links) == 1
    assert merge.find_duplicates(s) == []


def test_duplicates_page_merges(client, s):
    home, twin = _dup_station(s)
    s.commit()
    page = client.get("/settings/duplicates").text
    assert "Merge into the chosen one" in page
    r = client.post("/settings/duplicates", data={"_csrf": csrf(client), "entity": "station",
                                                  "keep": home.id, "ids": [home.id, twin.id]})
    assert r.status_code == 302
    s.expire_all()
    assert s.get(Station, twin.id).deleted_at is not None
    assert "No duplicates found" in client.get("/settings/duplicates").text
