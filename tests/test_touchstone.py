"""Touchstone parsing, SWR maths, sweep summary and SVG plot (services/touchstone.py)."""

import cmath
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from shacklog.services import touchstone as ts

DATA = Path(__file__).parent / "data" / "formats"


def _gamma(z: complex, z0: float = 50.0) -> complex:
    return (z - z0) / (z + z0)


def _dipole_lines(fmt: str = "RI", unit: str = "MHz", f0: float = 145.0, r: float = 50.0,
                  q: float = 12.0, n: int = 201, start: float = 140.0, stop: float = 150.0) -> list[str]:
    """Series-RLC model of a resonant antenna: Z = R + jRQ(f/f0 − f0/f)."""
    scale = {"Hz": 1e6, "kHz": 1e3, "MHz": 1.0, "GHz": 1e-3}[unit]
    lines = [f"# {unit} S {fmt} R 50"]
    for i in range(n):
        f = start + (stop - start) * i / (n - 1)
        g = _gamma(complex(r, r * q * (f / f0 - f0 / f)))
        if fmt == "RI":
            a, b = g.real, g.imag
        elif fmt == "MA":
            a, b = abs(g), math.degrees(cmath.phase(g))
        else:
            a, b = 20 * math.log10(max(abs(g), 1e-12)), math.degrees(cmath.phase(g))
        lines.append(f"{f * scale:.9g} {a:.9f} {b:.9f}")
    return lines


# ---------------------------------------------------------------------------
# Maths
# ---------------------------------------------------------------------------

def test_swr_return_loss_impedance():
    assert ts.swr(0) == 1.0
    assert ts.swr(complex(1 / 3, 0)) == pytest.approx(2.0)
    assert ts.swr(1) == math.inf and ts.swr(-1) == math.inf
    assert ts.return_loss_db(0.1) == pytest.approx(20.0)
    assert ts.return_loss_db(0) == math.inf
    assert ts.impedance(_gamma(complex(25, 10))) == pytest.approx(complex(25, 10))
    assert ts.impedance(0, 75.0) == 75.0
    assert ts.impedance(1).real == math.inf


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", ["RI", "MA", "DB"])
@pytest.mark.parametrize("unit", ["Hz", "kHz", "MHz", "GHz"])
def test_parse_formats_and_units(fmt, unit):
    parsed = ts.parse_touchstone("\n".join(_dipole_lines(fmt, unit)))
    assert parsed["z0"] == 50.0 and parsed["format"] == fmt and parsed["unit"] == unit
    assert len(parsed["freq_hz"]) == 201
    assert parsed["freq_hz"][0] == pytest.approx(140e6) and parsed["freq_hz"][-1] == pytest.approx(150e6)
    expected = _gamma(complex(50, 50 * 12 * (140 / 145 - 145 / 140)))
    assert parsed["s11"][0] == pytest.approx(expected, abs=1e-6)


def test_defaults_comments_blank_lines():
    text = """! no option line: GHz S MA R 50
    ! a comment
    0.1445 0.5 180   ! trailing comment

    0.1450 0.0 0
    """
    parsed = ts.parse_touchstone(text)
    assert parsed["freq_hz"] == [pytest.approx(144.5e6), pytest.approx(145e6)]
    assert parsed["s11"][0] == pytest.approx(-0.5 + 0j, abs=1e-9)
    assert (parsed["z0"], parsed["unit"], parsed["format"]) == (50.0, "GHz", "MA")


def test_option_line_any_order_and_z0():
    parsed = ts.parse_touchstone("#  r 75 ri mhz s\n145 0 0\n")
    assert parsed["z0"] == 75.0 and parsed["freq_hz"] == [145e6]


def test_s2p_takes_s11_and_handles_wrapped_rows():
    text = ("# MHz S RI R 50\n"
            "144 0.1 0.2 0.9 0 0.9 0 0.3 0.4\n"
            "145 0.0 0.1\n 0.9 0 0.9 0 0.3 0.4\n")
    parsed = ts.parse_touchstone(text)
    assert parsed["s11"] == [complex(0.1, 0.2), complex(0.0, 0.1)]


def test_unsorted_points_are_sorted_and_bom_is_ignored():
    parsed = ts.parse_touchstone("﻿# MHz S RI R 50\n146 0 0\n144 0.5 0\n")
    assert parsed["freq_hz"] == [144e6, 146e6]


@pytest.mark.parametrize("text, words", [
    ("", "No data"),
    ("# MHz S RI R 50\n! nothing\n", "No data"),
    ("# MHz Z RI R 50\n145 50 0\n", "not supported"),
    ("# MHz S RI R 50\n145 abc 0\n", "Line 2"),
    ("# MHz S RI R x\n145 0 0\n", "not a number"),
])
def test_errors_are_readable(text, words):
    with pytest.raises(ts.TouchstoneError, match=words):
        ts.parse_touchstone(text)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def test_summary_of_resonant_dipole_model():
    parsed = ts.parse_touchstone("\n".join(_dipole_lines(f0=145.0, r=50.0, q=12.0)))
    summ = ts.summarize(parsed)
    assert summ["points"] == 201
    assert (summ["start_mhz"], summ["stop_mhz"]) == (140.0, 150.0)
    assert summ["freq_min_mhz"] == pytest.approx(145.0)
    assert summ["min_swr"] == pytest.approx(1.0)
    assert summ["r_ohm"] == pytest.approx(50.0) and summ["x_ohm"] == pytest.approx(0.0, abs=1e-6)
    # SWR = 2 where |X| = R/sqrt(2) for R = Z0 → Q·(f/f0 − f0/f) = ±1/√2.
    k = 1 / (12 * math.sqrt(2))
    f_hi = 145 * (k + math.sqrt(k * k + 4)) / 2
    f_lo = 145 * (-k + math.sqrt(k * k + 4)) / 2
    assert summ["bw_swr2_low_mhz"] == pytest.approx(f_lo, abs=0.01)
    assert summ["bw_swr2_high_mhz"] == pytest.approx(f_hi, abs=0.01)


def test_summary_of_fixture_file():
    summ = ts.summarize(ts.parse_touchstone((DATA / "vertical_2m.s1p").read_text()))
    assert summ["freq_min_mhz"] == pytest.approx(145.2)
    assert summ["min_swr"] == pytest.approx(50 / 42, abs=1e-3)
    assert summ["r_ohm"] == pytest.approx(42.0, abs=0.01)
    assert summ["bw_swr2_low_mhz"] < 144.0 < 146.0 < summ["bw_swr2_high_mhz"] < 150.0


def test_summary_bad_match_has_no_bandwidth_and_edge_ranges():
    bad = ts.parse_touchstone("\n".join(_dipole_lines(r=200.0)))  # min SWR 4
    summ = ts.summarize(bad)
    assert summ["min_swr"] == pytest.approx(4.0)
    assert summ["bw_swr2_low_mhz"] is None and summ["bw_swr2_high_mhz"] is None
    # Low Q: SWR ≤ 2 across the whole sweep → range reported up to the sweep edges.
    wide = ts.summarize(ts.parse_touchstone("\n".join(_dipole_lines(q=0.5))))
    assert (wide["bw_swr2_low_mhz"], wide["bw_swr2_high_mhz"]) == (140.0, 150.0)
    assert ts.summarize({"freq_hz": [], "s11": [], "z0": 50.0})["points"] == 0


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------

def test_svg_structure():
    parsed = ts.parse_touchstone((DATA / "vertical_2m.s1p").read_text())
    svg = ts.swr_svg(parsed, width=640, height=260, shade=[(144.0, 146.0)])
    root = ET.fromstring(svg)  # well-formed XML
    ns = "{http://www.w3.org/2000/svg}"
    assert root.get("class") == "sweep-svg" and root.get("viewBox") == "0 0 640 260"
    assert len(root.findall(f"{ns}polyline[@class='curve']")) == 1
    assert len(root.findall(f"{ns}rect[@class='band']")) == 1
    grid = [el for el in root.iter() if "grid-line" in (el.get("class") or "").split()]
    assert len(grid) >= 5
    texts = [el.text for el in root.iter(f"{ns}text")]
    assert "1.5" in texts and "2" in texts and "3" in texts and "MHz" in texts
    assert any(t and t.startswith("145.200 MHz") for t in texts)  # marker label
    assert any(t in ("142", "144", "146", "148") for t in texts)  # frequency ticks
    assert root.find(f"{ns}circle") is not None
    # Themed by CSS: no hard-coded colours, no external resources.
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b|rgb\(|style=", svg)
    assert "http" not in svg.replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert all(el.get("fill") == "currentColor" for el in root.iter(f"{ns}text"))


def test_svg_clips_values_and_shade_outside_sweep():
    parsed = ts.parse_touchstone("\n".join(_dipole_lines(q=40.0)))  # SWR far above 5 at edges
    svg = ts.swr_svg(parsed, width=400, height=200, shade=[(430.0, 440.0)])
    root = ET.fromstring(svg)
    ns = "{http://www.w3.org/2000/svg}"
    assert root.find(f"{ns}rect[@class='band']") is None  # 70 cm band not in a 2 m sweep
    points = root.find(f"{ns}polyline").get("points").split()
    ys = [float(p.split(",")[1]) for p in points]
    assert min(ys) >= 14 - 1e-6 and max(ys) <= 200 - 30 + 1e-6  # inside the plot area


def test_svg_empty_and_single_point():
    assert "No data" in ts.swr_svg({"freq_hz": [], "s11": [], "z0": 50.0})
    ET.fromstring(ts.swr_svg({"freq_hz": [145e6], "s11": [0.2], "z0": 50.0}))
