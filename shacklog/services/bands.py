"""Bands, modes and frequency hints.

* :data:`BANDS` — ADIF band names with their ITU edges (ADIF 3.1 "Band" enumeration).
* :data:`MODES` — common ADIF modes with their submodes.
* :func:`service_hint` — what non-amateur service usually lives on a frequency (Region 1),
  used to pre-fill the listening log category.

Pure data and functions; no dependencies.
"""

from __future__ import annotations

#: (ADIF band name, lower MHz, upper MHz). Edges follow the ADIF enumeration.
BANDS: list[tuple[str, float, float]] = [
    ("2190m", 0.1357, 0.1378),
    ("630m", 0.472, 0.479),
    ("560m", 0.501, 0.504),
    ("160m", 1.8, 2.0),
    ("80m", 3.5, 4.0),
    ("60m", 5.06, 5.45),
    ("40m", 7.0, 7.3),
    ("30m", 10.1, 10.15),
    ("20m", 14.0, 14.35),
    ("17m", 18.068, 18.168),
    ("15m", 21.0, 21.45),
    ("12m", 24.89, 24.99),
    ("10m", 28.0, 29.7),
    ("8m", 40.0, 45.0),
    ("6m", 50.0, 54.0),
    ("5m", 54.000001, 69.9),
    ("4m", 70.0, 71.0),
    ("2m", 144.0, 148.0),
    ("1.25m", 222.0, 225.0),
    ("70cm", 420.0, 450.0),
    ("33cm", 902.0, 928.0),
    ("23cm", 1240.0, 1300.0),
    ("13cm", 2300.0, 2450.0),
    ("9cm", 3300.0, 3500.0),
    ("6cm", 5650.0, 5925.0),
    ("3cm", 10000.0, 10500.0),
    ("1.25cm", 24000.0, 24250.0),
    ("6mm", 47000.0, 47200.0),
    ("4mm", 75500.0, 81000.0),
    ("2.5mm", 119980.0, 123000.0),
    ("2mm", 134000.0, 149000.0),
    ("1mm", 241000.0, 250000.0),
    ("submm", 300000.0, 7500000.0),
]

BAND_NAMES: list[str] = [name for name, _, _ in BANDS]

#: ADIF mode → common submodes (subset of the ADIF 3.1 enumeration that a VHF/UHF/HF
#: station meets). Unknown modes are still accepted and stored as typed.
MODES: dict[str, list[str]] = {
    "FM": [],
    "AM": [],
    "SSB": ["USB", "LSB"],
    "CW": [],
    "DIGITALVOICE": ["DMR", "C4FM", "DSTAR", "FREEDV", "M17"],
    "FT8": [],
    "MFSK": ["FT4", "JS8", "Q65"],
    "RTTY": [],
    "PSK": ["PSK31", "PSK63", "QPSK31"],
    "PKT": [],
    "SSTV": [],
    "OLIVIA": [],
    "JT65": [],
    "MSK144": [],
    "DYNAMIC": ["VARA HF", "VARA FM 1200", "VARA FM 9600"],
}

#: Shortcuts people type → (ADIF mode, submode). "DMR" is a submode in ADIF.
MODE_ALIASES: dict[str, tuple[str, str | None]] = {
    "USB": ("SSB", "USB"),
    "LSB": ("SSB", "LSB"),
    "DMR": ("DIGITALVOICE", "DMR"),
    "C4FM": ("DIGITALVOICE", "C4FM"),
    "YSF": ("DIGITALVOICE", "C4FM"),
    "FUSION": ("DIGITALVOICE", "C4FM"),
    "DSTAR": ("DIGITALVOICE", "DSTAR"),
    "D-STAR": ("DIGITALVOICE", "DSTAR"),
    "M17": ("DIGITALVOICE", "M17"),
    "FREEDV": ("DIGITALVOICE", "FREEDV"),
    "FT4": ("MFSK", "FT4"),
    "JS8": ("MFSK", "JS8"),
    "Q65": ("MFSK", "Q65"),
    "NFM": ("FM", None),
    "WFM": ("FM", None),
    "PSK31": ("PSK", "PSK31"),
}

#: Ordered list for <select> widgets: what Andrea uses most first.
MODE_CHOICES: list[str] = ["FM", "AM", "SSB", "USB", "LSB", "CW", "DMR", "C4FM", "DSTAR",
                           "M17", "FT8", "FT4", "RTTY", "PSK31", "PKT", "SSTV"]

#: Rough non-amateur allocations (IARU Region 1 / Europe) for listening-log hints.
#: (low MHz, high MHz, swl category, description). First match wins.
SERVICES: list[tuple[float, float, str, str]] = [
    (0.148, 0.2835, "broadcast", "LW broadcast"),
    (0.5265, 1.6065, "broadcast", "MW broadcast"),
    (26.965, 27.405, "pmr", "CB 27 MHz"),
    (87.5, 108.0, "broadcast", "FM broadcast"),
    (108.0, 117.975, "airband", "Aeronautical navigation (VOR/ILS)"),
    (117.975, 137.0, "airband", "Aeronautical voice (AM)"),
    (137.0, 138.0, "weather", "Weather satellites (NOAA/Meteor)"),
    (144.8, 144.8, "data", "APRS (Europe)"),
    (156.0, 162.05, "marine", "Marine VHF"),
    (161.975, 162.025, "data", "AIS"),
    (400.15, 406.0, "weather", "Radiosondes"),
    (446.0, 446.2, "pmr", "PMR446"),
    (1090.0, 1090.0, "data", "ADS-B"),
]


def band_for_freq(mhz: float | None) -> str | None:
    """ADIF band name for a frequency in MHz (``145.5 → "2m"``), or None outside bands."""
    if mhz is None:
        return None
    for name, low, high in BANDS:
        if low <= mhz <= high:
            return name
    return None


def band_range(name: str) -> tuple[float, float] | None:
    """Edges of a band by ADIF name (case-insensitive)."""
    for band, low, high in BANDS:
        if band.lower() == (name or "").lower():
            return low, high
    return None


def normalize_mode(mode: str | None, submode: str | None = None) -> tuple[str | None, str | None]:
    """Map what was typed to ADIF (mode, submode): ``"DMR" → ("DIGITALVOICE", "DMR")``."""
    if not mode:
        return None, submode or None
    key = mode.strip().upper()
    if key in MODE_ALIASES:
        adif_mode, alias_sub = MODE_ALIASES[key]
        return adif_mode, (submode or alias_sub)
    return key, (submode.strip().upper() if submode else None)


def display_mode(mode: str | None, submode: str | None) -> str:
    """What to show in a list: the submode when there is one (``DMR``), else the mode."""
    return (submode or mode or "").upper()


def default_rst(mode: str | None) -> str:
    """Default report: 59 for phone, 599 for CW/RTTY/digital tone modes, -10 for FT8/FT4."""
    m = (mode or "").upper()
    if m in ("FT8", "FT4", "MFSK", "JT65", "Q65"):
        return "-10"
    if m in ("CW", "RTTY", "PSK", "PSK31"):
        return "599"
    return "59"


def service_hint(mhz: float | None) -> tuple[str, str] | None:
    """(swl category, description) for well-known services, or amateur band, or None."""
    if mhz is None:
        return None
    for low, high, category, label in SERVICES:
        if low <= mhz <= high:
            return category, label
    band = band_for_freq(mhz)
    if band:
        return "amateur", f"Amateur {band}"
    return None


def format_freq(mhz: float | None, digits: int = 4) -> str:
    """``145.5 → "145.5000"``; empty string for None. Used by templates."""
    if mhz is None:
        return ""
    return f"{mhz:.{digits}f}"
