"""Search: full-text hits with kind tabs and highlights, plus callsign, frequency and
locator shortcuts."""

from shacklog.models import JournalEntry, Qso, RadioChannel, RadioConfig, Repeater, SwlEntry, utcnow
from shacklog.web.views.search import frequency_of, looks_like_callsign


def _seed(s):
    s.add_all([
        Qso(call="IK2ABC", time_on=utcnow(), band="2m", mode="FM", freq_mhz=145.5,
            notes="Met on the club net, dipole on the balcony"),
        SwlEntry(time_utc=utcnow(), heard="F1XYZ", category="amateur", freq_mhz=430.5),
        JournalEntry(kind="test", time_utc=utcnow(), title="Dipole for 2 m",
                     body="Balcony dipole, first sweep"),
        Repeater(name="Milano R1", callsign="IR2UFD", output_mhz=145.6, input_mhz=145.0,
                 ctcss_up=88.5, locator="JN45ol"),
        Repeater(name="Bergamo RU5", callsign="IR2UBG", output_mhz=430.5, input_mhz=432.1),
    ])
    cfg = RadioConfig(title="UV-K5 summer", is_current=True)
    s.add(cfg)
    s.flush()
    s.add(RadioChannel(config_id=cfg.id, location=7, name="MI R1", freq_mhz=145.6))
    s.commit()


def test_helpers():
    assert looks_like_callsign("IK2ABC") and looks_like_callsign("F/IU2TST/P")
    assert looks_like_callsign("9A1A")
    assert not looks_like_callsign("DIPOLE") and not looks_like_callsign("145600")
    assert frequency_of("145.650") == 145.65 and frequency_of("145,65") == 145.65
    assert frequency_of("430500") == 430.5 and frequency_of("hello") is None


def test_empty_query_shows_help(client):
    for url in ("/search", "/search/", "/search?q="):
        r = client.get(url)
        assert r.status_code == 200 and "What can be searched" in r.text


def test_hits_tabs_and_highlight(client, s):
    _seed(s)
    page = client.get("/search?q=dipole").text
    assert 'aria-current="true">All <span class="small">2</span>' in page
    assert "QSOs <span" in page and "Journal <span" in page
    assert "<mark>dipole</mark>" in page.lower()
    assert "/journal/" in page and "/qsos/" in page
    only_journal = client.get("/search?q=dipole&kind=journal").text
    assert "Dipole for 2 m" in only_journal and "IK2ABC" not in only_journal
    nothing = client.get("/search?q=zzzqqq").text
    assert "Nothing found" in nothing


def test_callsign_shortcut(client, s):
    _seed(s)
    page = client.get("/search?q=ik2abc").text
    assert "All about IK2ABC" in page
    assert "/contacts/call/IK2ABC" in page and "/qsos/?call=IK2ABC" in page
    heard = client.get("/search?q=F1XYZ").text
    assert "Heard 1×" in heard and "/swl/?heard=F1XYZ" in heard
    assert "All about" not in client.get("/search?q=IZ9ZZZ").text  # not in any log


def test_frequency_shortcut(client, s):
    _seed(s)
    for text in ("145.600", "145,6", "145600", "145.6025"):
        page = client.get("/search", query_string={"q": text}).text
        assert "Repeaters within ±5 kHz" in page, text
        assert "Milano R1" in page and "UV-K5 summer" in page and "#7 MI R1" in page
        assert "Bergamo" not in page
    by_input = client.get("/search?q=145.000").text  # the repeater's input
    assert "Milano R1" in by_input
    page = client.get("/search?q=430.5").text
    assert "Bergamo RU5" in page and "Heard on it (1)" in page
    assert "Repeaters within" not in client.get("/search?q=2026").text  # bare number, no match


def test_locator_shortcut(client, s):
    _seed(s)
    page = client.get("/search?q=jn45").text
    assert "Show QSOs near JN45" in page and "Show repeaters near JN45" in page
    assert "near=JN45&amp;radius=50" in page
    assert "Locator JN45ol" in client.get("/search?q=JN45OL").text
    assert "Show QSOs near" not in client.get("/search?q=JN").text  # too vague
