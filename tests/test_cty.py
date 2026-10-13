"""Country lookup from AD1C's country files (services/cty.py)."""

from pathlib import Path

import pytest

from shacklog.services import cty

DATA = Path(__file__).parent / "data"


@pytest.fixture(params=["cty_sample.dat", "cty_sample.csv"])
def table(request):
    return cty.parse((DATA / request.param).read_text())


@pytest.mark.parametrize("call, name", [
    ("IK2ABC", "Italy"),
    ("iz2 abc", "Italy"),            # spaces and case don't matter
    ("IS0ABC", "Sardinia"),           # longer prefix wins over "I"
    ("IW0UAB", "Sardinia"),
    ("IG9ABC", "Italy"),              # *IG9 is only on the WAE list: DXCC says Italy
    ("EA8/DL1ABC", "Canary Islands"),
    ("DL1ABC/EA8", "Canary Islands"),
    ("DL1ABC/P", "Fed. Rep. of Germany"),
    ("UA9ABC", "Asiatic Russia"),
    ("UA1ABC", "European Russia"),
    ("UA3ABC/9", "Asiatic Russia"),   # another call area
    ("W1AW", "United States"),
    ("4U1UN", "United Nations HQ"),   # exact call
    ("II0GDF/J", "Italy"),
])
def test_lookup(table, call, name):
    assert table.lookup(call).name == name


def test_no_country(table):
    assert table.lookup("DL1ABC/MM") is None   # maritime mobile
    assert table.lookup("") is None and table.lookup(None) is None
    assert table.lookup("QQ1ABC") is None       # no such prefix


def test_details_and_overrides(table):
    italy = table.lookup("IK2ABC")
    assert (italy.continent, italy.cq_zone, italy.itu_zone) == ("EU", 15, 28)
    assert italy.longitude == pytest.approx(12.58)  # converted to east positive
    assert italy.utc_offset == 1.0
    assert italy.label == "Italy · EU · CQ 15 · ITU 28"
    special = table.lookup("IU0HTJ")
    assert (special.name, special.cq_zone, special.itu_zone) == ("Italy", 16, 29)
    west = table.lookup("W6ABC")
    assert (west.cq_zone, west.itu_zone) == (3, 6)
    assert table.lookup("UA1ABC/9").cq_zone == 18
    assert table.version == "20261001"
    assert "African Italy" not in {e.name for e in table.entities}  # WAE-only entity


def test_dxcc_numbers_only_in_csv():
    csv = cty.parse((DATA / "cty_sample.csv").read_text())
    dat = cty.parse((DATA / "cty_sample.dat").read_text())
    assert csv.lookup("IK2ABC").dxcc == 248 and dat.lookup("IK2ABC").dxcc is None
    # a comma inside the name doesn't confuse the csv reader
    assert csv.lookup("ZS8A").name == "Prince Edward & Marion Islands, test"


def test_location_part():
    assert cty.location_part("DL1ABC") == "DL1ABC"
    assert cty.location_part("W1ABC/4") == "W4ABC"
    assert cty.location_part("F/DL1ABC/P") == "F"
    assert cty.location_part("DL1ABC/AM") is None


def test_bad_files_are_refused(tmp_path):
    with pytest.raises(ValueError):
        cty.parse("hello world")
    with pytest.raises(ValueError):  # too few countries for a real list
        cty.install(tmp_path, (DATA / "cty_sample.csv").read_bytes())
    assert cty.installed_file(tmp_path) is None


def fake_list(count: int = 150) -> bytes:
    """A list long enough to be accepted by install()."""
    lines = [f"X{i},Country {i},{i},EU,14,28,50.0,-10.0,-1.0,X{i};" for i in range(count)]
    lines.append("I,Italy,248,EU,15,28,42.82,-12.58,-1.0,I IK IZ =VER20261001;")
    return "\n".join(lines).encode()


def test_install_and_lookup_from_the_data_folder(tmp_path):
    cty.configure(tmp_path)
    try:
        assert cty.lookup("IK2ABC") is None and cty.info(tmp_path) == {"installed": False}
        cty.install(tmp_path, fake_list())
        assert cty.lookup("IK2ABC").dxcc == 248
        info = cty.info(tmp_path)
        assert info["file"] == "cty.csv" and info["countries"] == 151
        assert info["version"] == "20261001"
        assert cty.update_if_due(tmp_path) is False  # fresh: no download attempted
    finally:
        cty.configure(None)
