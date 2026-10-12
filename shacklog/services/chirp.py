"""CHIRP CSV: read and write channel lists, repeater ↔ channel conversion, channel diff.

CHIRP (https://chirpmyradio.com) exports and imports a radio's memories as a CSV file with
a fixed header (:data:`CHIRP_COLUMNS`). ShackLog stores each row as a
:class:`~shacklog.models.RadioChannel` of a :class:`~shacklog.models.RadioConfig`, and can
generate a CSV from the repeater database to load into a radio (UV-K1, TM-733 …).

Rules learned from CHIRP's own CSV driver (``chirp/drivers/generic_csv.py``) that shape the
writer:

* an **empty** ``rToneFreq``, ``cToneFreq``, ``TStep`` or ``Power`` makes CHIRP reject the
  whole row, so the writer always fills them (88.5 Hz, step by band, :data:`DEFAULT_POWER`);
* ``Frequency``/``Offset`` are MHz with six decimals; ``Offset`` is a positive magnitude
  whose direction is in ``Duplex`` — except for ``Duplex = split``, where ``Offset`` is the
  **absolute transmit frequency** (cross-band repeaters);
* in ``TSQL`` mode CHIRP uses ``cToneFreq`` for both directions; in ``Tone`` mode only
  ``rToneFreq`` is sent. We write the tone in both columns for TSQL so either reading works.

Reading is forgiving: byte-order mark, CRLF, ``;`` separators and decimal commas (Excel in
an Italian locale), missing optional columns, extra columns. Every original column is kept
in ``"raw"`` so nothing is lost (MANDATES.md §3), and extra columns are written back.

Pure logic: no Flask, no database.
"""

from __future__ import annotations

import csv
import io
import json
import math
from typing import Any

#: The standard CHIRP CSV header, in CHIRP's order.
CHIRP_COLUMNS: list[str] = [
    "Location", "Name", "Frequency", "Duplex", "Offset", "Tone", "rToneFreq", "cToneFreq",
    "DtcsCode", "DtcsPolarity", "RxDtcsCode", "CrossMode", "Mode", "TStep", "Skip", "Power",
    "Comment", "URCALL", "RPT1CALL", "RPT2CALL", "DVCODE",
]

#: CSV column → RadioChannel attribute. Columns not listed (D-STAR ones) live in ``raw``.
COLUMN_FIELDS: dict[str, str] = {
    "Location": "location",
    "Name": "name",
    "Frequency": "freq_mhz",
    "Duplex": "duplex",
    "Offset": "offset_mhz",
    "Tone": "tone_mode",
    "rToneFreq": "rtone",
    "cToneFreq": "ctone",
    "DtcsCode": "dtcs",
    "DtcsPolarity": "dtcs_polarity",
    "RxDtcsCode": "rx_dtcs",
    "CrossMode": "cross_mode",
    "Mode": "mode",
    "TStep": "tstep",
    "Skip": "skip",
    "Power": "power",
    "Comment": "comment",
}

#: RadioChannel attributes that describe a channel (compared by :func:`diff_channels`).
CHANNEL_FIELDS: list[str] = list(COLUMN_FIELDS.values())

DEFAULT_TONE = 88.5
DEFAULT_DTCS = "023"
DEFAULT_POLARITY = "NN"
DEFAULT_CROSS_MODE = "Tone->Tone"
DEFAULT_MODE = "FM"
#: CHIRP rejects a row with an empty Power. 5 W is a safe choice: CHIRP maps it to the
#: nearest level the radio has (low on a mobile, high on a handheld).
DEFAULT_POWER = "5.0W"

#: Shift beyond which input and output are on different bands (cross-band, "split").
CROSS_BAND_MHZ = 70.0

_DUPLEX_VALUES = {"", "+", "-", "split", "off"}
_TONE_MODES = {"": "", "tone": "Tone", "tsql": "TSQL", "dtcs": "DTCS", "dtcs-r": "DTCS-R",
               "tsql-r": "TSQL-R", "cross": "Cross"}

#: Repeater kind → CHIRP mode. Analogue nodes are plain FM on the air.
_KIND_TO_MODE = {"FM": "FM", "ECHOLINK": "FM", "ALLSTAR": "FM", "DMR": "DMR",
                 "C4FM": "DN", "DSTAR": "DV"}
_MODE_TO_KIND = {"DMR": "DMR", "DN": "C4FM", "DV": "DSTAR"}


# ---------------------------------------------------------------------------
# Small value helpers
# ---------------------------------------------------------------------------

def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from a dict or an object (RadioChannel, Repeater) alike."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _float(value: Any) -> float | None:
    """Parse a number; tolerates a decimal comma, units and blanks. None if not a number."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    text = str(value).strip().replace(",", ".")
    for unit in ("mhz", "hz", "w"):
        if text.lower().endswith(unit):
            text = text[: -len(unit)].strip()
            break
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _freq_mhz(value: Any) -> float | None:
    """A CHIRP frequency in MHz. Values that can only be Hz (≥ 1 000 000) are converted."""
    number = _float(value)
    if number is None or number <= 0:
        return None
    if number >= 1_000_000:
        number /= 1_000_000
    return round(number, 6)


def _dtcs(value: Any) -> str | None:
    """``"23"`` → ``"023"``. Non-numeric codes are kept as typed."""
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    return f"{int(text):03d}" if text.isdigit() else text


#: Tuning steps (kHz) tried when a frequency is off the band's usual raster.
_STEPS_KHZ = (12.5, 25.0, 6.25, 5.0, 10.0, 2.5, 1.0, 0.5, 0.25, 0.1, 0.01)


def _band_tstep(freq_mhz: float | None) -> str:
    """Default TStep: 12.5 kHz on 2 m / 70 cm (IARU Region 1 raster), else 5 kHz.

    When the frequency is not a multiple of that step (PMR446 at 446.00625, an airband
    8.33 kHz channel…) the first step that divides it exactly is used instead, because
    radios round the frequency to the step.
    """
    preferred = 5.0
    if freq_mhz is not None and (144 <= freq_mhz <= 148 or 420 <= freq_mhz <= 450):
        preferred = 12.5
    if freq_mhz is None:
        return f"{preferred:.2f}"
    hz = round(freq_mhz * 1_000_000)
    for step in (preferred, *_STEPS_KHZ):
        if hz % round(step * 1000) == 0:
            return f"{step:.2f}"
    return f"{preferred:.2f}"


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text


def _raw_dict(raw: Any) -> dict[str, str]:
    """``raw`` as a dict whether it is a dict or the JSON text stored in RadioChannel.raw."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return {str(k): "" if v is None else str(v) for k, v in raw.items()}
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return {str(k): "" if v is None else str(v) for k, v in loaded.items()} if isinstance(loaded, dict) else {}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _sniff_delimiter(first_line: str) -> str:
    # Excel in Italian/French locales saves "CSV" with semicolons.
    return ";" if first_line.count(";") > first_line.count(",") else ","


def read_chirp_csv(text: str) -> list[dict]:
    """Parse a CHIRP CSV into channel dicts (keys = RadioChannel fields, plus ``"raw"``).

    ``raw`` is the original row (header → text) including unknown columns. Rows without a
    usable frequency (empty memories, notes) are skipped. A missing or invalid Location
    continues the numbering from the previous row.
    """
    if isinstance(text, bytes):  # be kind to callers that pass the upload as-is
        try:
            text = text.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = text.decode("latin-1")
    text = text.lstrip("﻿")
    lines = text.splitlines()
    first = next((ln for ln in lines if ln.strip()), "")
    if not first:
        return []
    delimiter = _sniff_delimiter(first)
    decimal_comma = delimiter == ";"
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    header: list[str] | None = None
    channels: list[dict] = []
    last_location = -1
    canonical = {c.lower(): c for c in CHIRP_COLUMNS}
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        if header is None:
            header = [cell.strip() for cell in row]
            continue
        raw = {header[i] if i < len(header) else f"Column{i + 1}": cell
               for i, cell in enumerate(row)}
        # Look columns up case-insensitively: "frequency" and "Frequency" are the same.
        values = {canonical.get(k.lower(), k): v for k, v in raw.items()}
        if decimal_comma:
            values = {k: v.replace(",", ".") if k in ("Frequency", "Offset", "rToneFreq",
                                                      "cToneFreq", "TStep") else v
                      for k, v in values.items()}
        channel = _row_to_channel(values)
        if channel["freq_mhz"] is None:
            continue
        if channel["location"] is None:
            channel["location"] = last_location + 1
        last_location = channel["location"]
        channel["raw"] = raw
        channels.append(channel)
    return channels


def _row_to_channel(values: dict[str, str]) -> dict:
    location_text = (values.get("Location") or "").strip()
    try:
        location = int(float(location_text)) if location_text else None
    except ValueError:
        location = None
    duplex = (values.get("Duplex") or "").strip().lower()
    tone_key = (values.get("Tone") or "").strip().lower()
    return {
        "location": location,
        "name": (values.get("Name") or "").strip(),
        "freq_mhz": _freq_mhz(values.get("Frequency")),
        "duplex": duplex if duplex in _DUPLEX_VALUES else (values.get("Duplex") or "").strip(),
        "offset_mhz": _float(values.get("Offset")),
        "tone_mode": _TONE_MODES.get(tone_key, (values.get("Tone") or "").strip()),
        "rtone": _float(values.get("rToneFreq")),
        "ctone": _float(values.get("cToneFreq")),
        "dtcs": _dtcs(values.get("DtcsCode")),
        "dtcs_polarity": _clean_str(values.get("DtcsPolarity")) or None,
        "rx_dtcs": _dtcs(values.get("RxDtcsCode")),
        "cross_mode": _clean_str(values.get("CrossMode")) or None,
        "mode": _clean_str(values.get("Mode")) or None,
        "tstep": _clean_str(values.get("TStep")) or None,
        "skip": _clean_str(values.get("Skip")) or "",
        "power": _clean_str(values.get("Power")) or None,
        "comment": _clean_str(values.get("Comment")) or "",
    }


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _format_tone(value: Any) -> str:
    number = _float(value)
    return f"{number if number is not None else DEFAULT_TONE:.1f}"


def _format_tstep(value: Any, freq: float | None) -> str:
    number = _float(value)
    return f"{number:.2f}" if number is not None and number > 0 else _band_tstep(freq)


def channel_to_row(channel: Any, location: int | None = None) -> dict[str, str]:
    """One channel (dict or RadioChannel) → CSV row dict (header → text), with defaults."""
    raw = _raw_dict(_get(channel, "raw"))
    raw_lower = {k.lower(): v for k, v in raw.items()}
    freq = _freq_mhz(_get(channel, "freq_mhz"))
    duplex = (_get(channel, "duplex") or "").strip()
    offset = _float(_get(channel, "offset_mhz")) or 0.0
    if duplex in ("+", "-"):
        offset = abs(offset)  # the sign lives in Duplex
    loc = _get(channel, "location")
    row = {
        "Location": str(loc if loc is not None else (location if location is not None else "")),
        "Name": (_get(channel, "name") or "").strip(),
        "Frequency": f"{freq:.6f}" if freq is not None else "",
        "Duplex": duplex,
        "Offset": f"{offset:.6f}",
        "Tone": _get(channel, "tone_mode") or "",
        "rToneFreq": _format_tone(_get(channel, "rtone")),
        "cToneFreq": _format_tone(_get(channel, "ctone")),
        "DtcsCode": _dtcs(_get(channel, "dtcs")) or DEFAULT_DTCS,
        "DtcsPolarity": _get(channel, "dtcs_polarity") or DEFAULT_POLARITY,
        "RxDtcsCode": _dtcs(_get(channel, "rx_dtcs")) or _dtcs(_get(channel, "dtcs")) or DEFAULT_DTCS,
        "CrossMode": _get(channel, "cross_mode") or DEFAULT_CROSS_MODE,
        "Mode": _get(channel, "mode") or DEFAULT_MODE,
        "TStep": _format_tstep(_get(channel, "tstep"), freq),
        "Skip": _get(channel, "skip") or "",
        "Power": _get(channel, "power") or DEFAULT_POWER,
        "Comment": _get(channel, "comment") or "",
    }
    # D-STAR columns and unknown columns come from the original row.
    for column in CHIRP_COLUMNS:
        if column not in row:
            row[column] = raw_lower.get(column.lower(), "")
    standard = {c.lower() for c in CHIRP_COLUMNS}
    for key, value in raw.items():
        if key.lower() not in standard:
            row[key] = value
    return row


def write_chirp_csv(channels: list[dict]) -> str:
    """Write channels (dicts or RadioChannel objects) as a CHIRP CSV.

    The header is :data:`CHIRP_COLUMNS` followed by any extra columns found in the
    channels' ``raw`` data (first-seen order). Channels without a location are numbered
    after the highest one. Lines end with CRLF like CHIRP's own files.
    """
    known = [_get(c, "location") for c in channels if _get(c, "location") is not None]
    next_location = (max(known) + 1) if known else 1
    rows: list[dict[str, str]] = []
    extra_columns: list[str] = []
    standard = {c.lower() for c in CHIRP_COLUMNS}
    for channel in channels:
        location = None
        if _get(channel, "location") is None:
            location, next_location = next_location, next_location + 1
        row = channel_to_row(channel, location)
        for key in row:
            if key.lower() not in standard and key not in extra_columns:
                extra_columns.append(key)
        rows.append(row)
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CHIRP_COLUMNS + extra_columns, restval="",
                            extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Repeater ↔ channel
# ---------------------------------------------------------------------------

def blank_channel(location: int) -> dict:
    """A channel dict with CHIRP's neutral defaults (simplex FM, no tone)."""
    return {
        "location": location, "name": "", "freq_mhz": None, "duplex": "", "offset_mhz": 0.0,
        "tone_mode": "", "rtone": DEFAULT_TONE, "ctone": DEFAULT_TONE, "dtcs": DEFAULT_DTCS,
        "dtcs_polarity": DEFAULT_POLARITY, "rx_dtcs": DEFAULT_DTCS,
        "cross_mode": DEFAULT_CROSS_MODE, "mode": DEFAULT_MODE, "tstep": None, "skip": "",
        "power": None, "comment": "",
    }


def repeater_to_channel(rep: Any, location: int, name_length: int = 10) -> dict:
    """A channel dict for a repeater (``Repeater`` or any object/dict with its fields).

    * Name: ident (``R1``), else callsign, else name — cut to ``name_length`` characters
      (10 fits the UV-K1/UV-K5 display).
    * Frequency: the repeater output (what I listen to). Duplex/Offset from input − output;
      a shift above 70 MHz is cross-band, written as ``split`` with Offset = input.
    * Tones: DCS → ``DTCS``; equal up/down CTCSS → ``TSQL``; different → ``Cross``
      (``Tone->Tone``); only an access tone → ``Tone``. A repeater that only *sends* a
      tone needs none from me: ``""`` (open squelch), so the channel always works.
    * Mode: FM for analogue kinds (FM, EchoLink, AllStar); DMR/C4FM/D-STAR map to CHIRP's
      ``DMR``/``DN``/``DV``; anything else is passed through (callers filter by kind).
    """
    ch = blank_channel(location)
    ident = (_get(rep, "ident") or "").strip()
    callsign = (_get(rep, "callsign") or "").strip()
    name = (_get(rep, "name") or "").strip()
    ch["name"] = (ident or callsign or name)[:name_length].strip()

    output = _float(_get(rep, "output_mhz"))
    input_ = _float(_get(rep, "input_mhz"))
    ch["freq_mhz"] = round(output, 6) if output is not None else None
    if output is not None and input_ is not None:
        shift = round(input_ - output, 6)
        if abs(shift) > CROSS_BAND_MHZ:
            ch["duplex"], ch["offset_mhz"] = "split", round(input_, 6)
        elif abs(shift) >= 1e-6:
            ch["duplex"], ch["offset_mhz"] = ("+" if shift > 0 else "-"), abs(shift)

    up = _float(_get(rep, "ctcss_up"))
    down = _float(_get(rep, "ctcss_down"))
    dcs = _dtcs(_get(rep, "dcs_code"))
    if dcs:
        ch["tone_mode"], ch["dtcs"], ch["rx_dtcs"] = "DTCS", dcs, dcs
    elif up is not None and down is not None and abs(up - down) < 0.05:
        ch["tone_mode"], ch["rtone"], ch["ctone"] = "TSQL", up, up
    elif up is not None and down is not None:
        ch["tone_mode"], ch["rtone"], ch["ctone"] = "Cross", up, down
        ch["cross_mode"] = "Tone->Tone"
    elif up is not None:
        ch["tone_mode"], ch["rtone"] = "Tone", up

    kind = (_get(rep, "kind") or "FM").upper()
    ch["mode"] = _KIND_TO_MODE.get(kind, kind)
    ch["tstep"] = _band_tstep(ch["freq_mhz"])
    location_name = (_get(rep, "location_name") or "").strip()
    bits = [b for b in (callsign, location_name) if b and b != ch["name"]]
    if not bits and name and name != ch["name"]:
        bits = [name]
    ch["comment"] = " ".join(bits)
    return ch


def channel_to_repeater_fields(ch: Any) -> dict:
    """Repeater field values from a channel — the inverse of :func:`repeater_to_channel`.

    Returns ``output_mhz``, ``input_mhz`` (None when TX is off), ``ctcss_up``,
    ``ctcss_down``, ``dcs_code``, ``name``, ``notes`` (the comment) and ``kind``.
    """
    freq = _freq_mhz(_get(ch, "freq_mhz"))
    duplex = (_get(ch, "duplex") or "").strip().lower()
    offset = _float(_get(ch, "offset_mhz")) or 0.0
    if freq is None:
        input_ = None
    elif duplex == "+":
        input_ = round(freq + abs(offset), 6)
    elif duplex == "-":
        input_ = round(freq - abs(offset), 6)
    elif duplex == "split":
        input_ = round(offset, 6) if offset else None
    elif duplex == "off":
        input_ = None  # receive only (a beacon, the ISS downlink…)
    else:
        input_ = freq

    tone_mode = (_get(ch, "tone_mode") or "").strip()
    rtone, ctone = _float(_get(ch, "rtone")), _float(_get(ch, "ctone"))
    up = down = None
    dcs = None
    if tone_mode == "Tone":
        up = rtone
    elif tone_mode == "TSQL":
        # CHIRP uses cToneFreq for both directions in TSQL; old files only set rToneFreq.
        up = down = ctone if ctone is not None else rtone
    elif tone_mode in ("DTCS", "DTCS-R"):
        dcs = _dtcs(_get(ch, "dtcs"))
    elif tone_mode == "Cross":
        tx, _, rx = (_get(ch, "cross_mode") or DEFAULT_CROSS_MODE).partition("->")
        if tx == "Tone":
            up = rtone
        elif tx == "DTCS":
            dcs = _dtcs(_get(ch, "dtcs"))
        if rx == "Tone":
            down = ctone
        elif rx == "DTCS" and dcs is None:
            dcs = _dtcs(_get(ch, "rx_dtcs")) or _dtcs(_get(ch, "dtcs"))

    mode = (_get(ch, "mode") or "FM").strip().upper()
    return {
        "output_mhz": freq,
        "input_mhz": input_,
        "ctcss_up": up,
        "ctcss_down": down,
        "dcs_code": dcs,
        "name": (_get(ch, "name") or "").strip(),
        "notes": (_get(ch, "comment") or "").strip() or None,
        "kind": _MODE_TO_KIND.get(mode, "FM"),
    }


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

#: Text fields holding numbers: "12.5" and "12.50" are the same step.
_NUMERIC_TEXT = {"tstep"}


def _same(a: Any, b: Any, name: str = "") -> bool:
    """Equality that treats None and "" alike and ignores float noise."""
    if a in (None, "") and b in (None, ""):
        return True
    if name in _NUMERIC_TEXT and _float(a) is not None and _float(b) is not None:
        return abs(_float(a) - _float(b)) < 1e-6
    fa, fb = (a if isinstance(a, (int, float)) else None), (b if isinstance(b, (int, float)) else None)
    if fa is not None and fb is not None:
        return abs(float(fa) - float(fb)) < 1e-6
    return str(a).strip() == str(b).strip()


def diff_channels(old: list[dict], new: list[dict]) -> dict:
    """Compare two channel lists by Location.

    Returns ``{"added": [new…], "removed": [old…], "changed": [(old, new, [field…])]}``,
    each sorted by location. ``raw`` (and any non-channel key) is ignored.
    """
    old_by = {_get(c, "location"): c for c in old}
    new_by = {_get(c, "location"): c for c in new}
    added = [new_by[k] for k in sorted(new_by.keys() - old_by.keys(), key=_sort_key)]
    removed = [old_by[k] for k in sorted(old_by.keys() - new_by.keys(), key=_sort_key)]
    changed = []
    for key in sorted(old_by.keys() & new_by.keys(), key=_sort_key):
        a, b = old_by[key], new_by[key]
        fields = [f for f in CHANNEL_FIELDS if f != "location" and not _same(_get(a, f), _get(b, f), f)]
        if fields:
            changed.append((a, b, fields))
    return {"added": added, "removed": removed, "changed": changed}


def _sort_key(location: Any) -> tuple[int, Any]:
    # Locations are ints, but be safe with a None from a hand-built list.
    return (0, location) if isinstance(location, int) else (1, str(location))
