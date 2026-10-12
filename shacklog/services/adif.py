"""ADIF (ADI text format): parse, write, and map records to/from :class:`~shacklog.models.Qso`.

Layers
------
* :func:`parse_adi` / :func:`write_adi` know only the file format: they turn text into
  ``(header, [record, …])`` where every header and record is a ``{field_name: value}``
  dict with lower-case names, and back.
* :func:`record_to_qso_fields` / :func:`qso_to_record` translate between an ADIF record and
  ShackLog's ``Qso`` columns. Fields ShackLog has no column for come back as *extras*;
  ``services/qso_io.py`` stores them in ``Qso.extra_adif`` and puts them back on export, so a
  file that goes through ShackLog loses nothing (MANDATES.md §3, "unknown data is preserved").

Format notes (ADIF 3.1.5, "ADI File Format")
--------------------------------------------
* A data specifier is ``<NAME:LENGTH>`` or ``<NAME:LENGTH:TYPE>`` followed by exactly LENGTH
  characters. Anything between specifiers is free text and is ignored.
* A file whose first character is not ``<`` starts with a header that ends at ``<EOH>``.
* ``<EOR>`` ends a record. Names and markers are case-insensitive.
* The spec counts **characters**, but many programs count **UTF-8 bytes**. We parse the
  decoded text and, only when a value contains non-ASCII characters and the character count
  does not land on a field boundary, retry that one field counting bytes. See
  :func:`_read_value`.

Pure logic: no Flask, no database.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, time, timedelta, timezone

from shacklog import APP_NAME, __version__
from shacklog.services import geo

ADIF_VERSION = "3.1.5"

#: Private fields ShackLog writes so that its own exports re-import cleanly.
APP_ID_FIELD = "app_shacklog_id"
APP_TAGS_FIELD = "app_shacklog_tags"

# A data specifier or marker. Names may hold anything but ``: < > { } ,`` (the spec allows
# spaces inside application-defined names); the optional type indicator is one letter.
_TAG_RE = re.compile(r"<([^\s:<>{},][^:<>{},]*?)(?::(\d+)(?::([A-Za-z]))?)?>")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def decode_bytes(data: bytes | str) -> tuple[str, bool]:
    """Bytes → text. Returns ``(text, utf8)``; ``utf8`` is False for the latin-1 fallback.

    Latin-1 never fails, so a file from an old Windows logger still opens. In that case
    one byte is one character and the byte-count retry in :func:`_read_value` is pointless.
    """
    if isinstance(data, str):
        return data.lstrip("﻿"), True
    try:
        return data.decode("utf-8-sig"), True
    except UnicodeDecodeError:
        return data.decode("latin-1"), False


def _is_boundary(text: str, index: int) -> bool:
    """True if a value ending at ``index`` is followed by what normally follows a value."""
    return index >= len(text) or text[index].isspace() or text[index] == "<"


def _take_utf8_bytes(text: str, start: int, nbytes: int) -> str | None:
    """The prefix of ``text[start:]`` that is exactly ``nbytes`` long in UTF-8.

    None when no character boundary falls exactly there (so the length can't have been a
    UTF-8 byte count). A text that ends early returns what there is, like slicing would.
    """
    total = 0
    index = start
    while total < nbytes and index < len(text):
        total += len(text[index].encode("utf-8", "surrogatepass"))
        index += 1
    if total > nbytes:
        return None
    return text[start:index]


def _read_value(text: str, start: int, length: int, utf8_retry: bool) -> tuple[str, int]:
    """Read one field value; returns ``(value, index after it)``.

    The spec (character count) is the first choice. The UTF-8 byte count wins only when the
    value contains non-ASCII characters and either the character count runs past a field
    boundary while the byte count stops right on one, or the character count's extra
    characters are nothing but the whitespace separating two fields.
    """
    by_chars = text[start:start + length]
    if by_chars.isascii() or not utf8_retry:
        return by_chars, start + len(by_chars)
    by_bytes = _take_utf8_bytes(text, start, length)
    if by_bytes is None or by_bytes == by_chars:
        return by_chars, start + len(by_chars)
    chars_ok = _is_boundary(text, start + len(by_chars))
    bytes_ok = _is_boundary(text, start + len(by_bytes))
    overshoot_is_space = by_chars[len(by_bytes):].strip() == ""
    if bytes_ok and (not chars_ok or overshoot_is_space):
        return by_bytes, start + len(by_bytes)
    return by_chars, start + len(by_chars)


def parse_adi(data: bytes | str) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Parse an ADI file into ``(header, records)``. Field names are lower case.

    Tolerates: no header, free text and comments between fields, ``<EOH>``/``<EOR>`` in any
    case, type indicators, CRLF line ends, a missing final ``<EOR>`` and UTF-8 byte counts.
    Empty values (``<NAME:0>``) are dropped because they carry no information. When a field
    appears twice in a record the last one wins.
    """
    text, utf8 = decode_bytes(data)
    header: dict[str, str] = {}
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    pos = 0
    while True:
        lt = text.find("<", pos)
        if lt < 0:
            break
        match = _TAG_RE.match(text, lt)
        if match is None:  # a stray "<" in free text
            pos = lt + 1
            continue
        name = match.group(1).strip().lower()
        length = match.group(2)
        pos = match.end()
        if length is None:
            if name == "eoh":
                # Only a marker before the first record can close a header. Some programs
                # write a header without the leading free text the spec asks for.
                if not records:
                    header = current
                current = {}
            elif name == "eor":
                if current:
                    records.append(current)
                current = {}
            # Anything else without a length (``<program>``) is free text.
            continue
        value, pos = _read_value(text, pos, int(length), utf8)
        if value != "":
            current[name] = value
    if current:  # last record without <EOR>
        records.append(current)
    return header, records


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _field(name: str, value: str) -> str:
    # The spec counts characters, so len() of the Python string is the right number.
    return f"<{name.upper()}:{len(value)}>{value}"


def write_adi(records: list[dict[str, str]], header: dict[str, str] | None = None) -> str:
    """Write records as an ADI file (one record per line, ending with ``<eor>``).

    The header always carries ADIF_VER, PROGRAMID, PROGRAMVERSION and CREATED_TIMESTAMP;
    ``header`` adds or overrides fields. Empty and None values are skipped.
    """
    head: dict[str, str] = {
        "adif_ver": ADIF_VERSION,
        "programid": APP_NAME,
        "programversion": __version__,
        "created_timestamp": datetime.now(timezone.utc).strftime("%Y%m%d %H%M%S"),
    }
    for key, value in (header or {}).items():
        head[key.lower()] = value
    # The free text before the first field is what tells readers a header is present.
    count = len(records)
    lines = [f"{APP_NAME} ADIF export, {count} {'record' if count == 1 else 'records'}"]
    lines += [_field(k, str(v)) for k, v in head.items() if v not in (None, "")]
    lines += ["<eoh>", ""]
    for rec in records:
        fields = [_field(k, str(v)) for k, v in rec.items() if v not in (None, "")]
        lines.append(" ".join(fields + ["<eor>"]))
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# Value conversions
# ---------------------------------------------------------------------------

def parse_number(value: str | None) -> float | None:
    """ADIF Number → float; tolerates a decimal comma. None if invalid or not finite."""
    if value is None:
        return None
    text = value.strip().replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _parse_power(value: str) -> float | None:
    # Some loggers write "5W" or "50 w" although ADIF wants a bare number of watts.
    number = parse_number(re.sub(r"\s*[wW]\s*$", "", value))
    return number if number is not None and number >= 0 else None


def _parse_freq(value: str) -> float | None:
    number = parse_number(value)
    return number if number is not None and number > 0 else None


_LOCATION_RE = re.compile(r"^\s*([NSEW])\s*(\d{1,3})\s+(\d{1,2}(?:\.\d*)?)\s*$", re.IGNORECASE)


def parse_location(value: str | None, axis: str) -> float | None:
    """ADIF Location ``"XDDD MM.MMM"`` → decimal degrees. ``axis`` is ``"lat"`` or ``"lon"``.

    Plain decimal degrees (written by some programs against the spec) are accepted too.
    """
    if not value:
        return None
    hemispheres = "NS" if axis == "lat" else "EW"
    limit = 90.0 if axis == "lat" else 180.0
    match = _LOCATION_RE.match(value)
    if match:
        hem, deg, minutes = match.group(1).upper(), int(match.group(2)), float(match.group(3))
        if hem not in hemispheres or minutes >= 60:
            return None
        result = deg + minutes / 60.0
        if hem in "SW":
            result = -result
    else:
        result = parse_number(value)
        if result is None:
            return None
    return round(result, 6) if abs(result) <= limit else None


def format_location(value: float, axis: str) -> str:
    """Decimal degrees → ADIF ``"N045 27.840"`` / ``"E009 11.400"``."""
    hem = ("N" if value >= 0 else "S") if axis == "lat" else ("E" if value >= 0 else "W")
    total_minutes = round(abs(value) * 60.0, 3)
    degrees = int(total_minutes // 60)
    minutes = total_minutes - degrees * 60
    return f"{hem}{degrees:03d} {minutes:06.3f}"


def format_number(value: float, decimals: int = 6) -> str:
    """``145.5 → "145.5"``, ``5.0 → "5"``: no trailing zeros, at most ``decimals`` places."""
    text = f"{value:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _parse_date(value: str | None) -> date | None:
    text = (value or "").strip()
    if not re.fullmatch(r"\d{8}", text):
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def _parse_time(value: str | None) -> time | None:
    text = (value or "").strip().replace(":", "")
    if not re.fullmatch(r"\d{4}|\d{6}", text):
        return None
    try:
        return time(int(text[:2]), int(text[2:4]), int(text[4:6] or 0))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# ADIF record ↔ Qso fields
# ---------------------------------------------------------------------------

#: (ADIF field, Qso attribute, kind). Kinds: str, call, upper, lower, freq, power,
#: locator, lat, lon, qsl. Date/time and the private id are handled separately.
FIELD_MAP: list[tuple[str, str, str]] = [
    ("call", "call", "call"),
    ("freq", "freq_mhz", "freq"),
    ("freq_rx", "freq_rx_mhz", "freq"),
    ("band", "band", "lower"),
    ("mode", "mode", "upper"),
    ("submode", "submode", "upper"),
    ("rst_sent", "rst_sent", "str"),
    ("rst_rcvd", "rst_rcvd", "str"),
    ("tx_pwr", "tx_power_w", "power"),
    ("name", "name", "str"),
    ("qth", "qth", "str"),
    ("gridsquare", "locator", "locator"),
    ("lat", "latitude", "lat"),
    ("lon", "longitude", "lon"),
    ("country", "country", "str"),
    ("state", "region", "str"),
    ("station_callsign", "my_call", "call"),
    ("operator", "operator", "call"),
    ("my_gridsquare", "my_locator", "locator"),
    ("my_lat", "my_latitude", "lat"),
    ("my_lon", "my_longitude", "lon"),
    ("prop_mode", "prop_mode", "upper"),
    ("sat_name", "sat_name", "str"),
    ("sat_mode", "sat_mode", "str"),
    ("qsl_sent", "qsl_sent", "qsl"),
    ("qsl_rcvd", "qsl_rcvd", "qsl"),
    ("qsl_via", "qsl_via", "str"),
    ("lotw_qsl_sent", "lotw_sent", "qsl"),
    ("lotw_qsl_rcvd", "lotw_rcvd", "qsl"),
    ("eqsl_qsl_sent", "eqsl_sent", "qsl"),
    ("eqsl_qsl_rcvd", "eqsl_rcvd", "qsl"),
    ("sota_ref", "sota_ref", "str"),
    ("pota_ref", "pota_ref", "str"),
    ("wwff_ref", "wwff_ref", "str"),
    ("my_sota_ref", "my_sota_ref", "str"),
    ("my_pota_ref", "my_pota_ref", "str"),
    ("my_wwff_ref", "my_wwff_ref", "str"),
    ("contest_id", "contest_id", "str"),
    ("comment", "comment", "str"),
    ("notes", "notes", "str"),
    (APP_TAGS_FIELD, "tags", "str"),
]

_DATETIME_FIELDS = {"qso_date", "time_on", "qso_date_off", "time_off"}

# Human words for problem messages.
_KIND_PROBLEM = {
    "freq": "is not a frequency in MHz",
    "power": "is not a power in watts",
    "locator": "is not a Maidenhead locator",
    "lat": "is not an ADIF latitude",
    "lon": "is not an ADIF longitude",
    "qsl": "is not a QSL status",
}


def _convert(kind: str, raw: str):
    """Convert one ADIF value for its kind. Returns None when the value is invalid."""
    text = raw.strip()
    if kind == "str":
        return text
    if kind == "call":
        return text.replace(" ", "").upper()
    if kind == "upper":
        return text.upper()
    if kind == "lower":
        return text.lower()
    if kind == "freq":
        return _parse_freq(text)
    if kind == "power":
        return _parse_power(text)
    if kind == "locator":
        return geo.normalize_locator(text)
    if kind in ("lat", "lon"):
        return parse_location(text, kind)
    if kind == "qsl":
        # ADIF QSL statuses are single letters (Y, N, R, Q, I, V); the column holds two.
        return text.upper() if len(text) <= 2 else None
    raise ValueError(f"unknown kind {kind!r}")


def record_to_qso_fields(rec: dict[str, str], problems: list[str] | None = None) -> tuple[dict, dict]:
    """Map an ADIF record to ``(Qso field values, extra ADIF fields)``.

    Only fields with a usable value appear in the first dict. An invalid number, locator,
    position or date leaves the Qso field unset and keeps the raw value in the extras, so
    it is exported again unchanged. ``problems`` (optional) collects a sentence per such
    value. ``app_shacklog_id`` stays in the extras for ``qso_io`` to recognise its own QSOs.
    """
    fields: dict = {}
    extras: dict[str, str] = {}
    mapped = {adif_name for adif_name, _, _ in FIELD_MAP} | _DATETIME_FIELDS

    def problem(name: str, raw: str, why: str) -> None:
        extras[name] = raw
        if problems is not None:
            problems.append(f'{name.upper()} "{raw}" {why}; kept unchanged as an extra field.')

    for adif_name, attr, kind in FIELD_MAP:
        raw = rec.get(adif_name)
        if raw is None or raw.strip() == "":
            continue
        value = _convert(kind, raw)
        if value is None:
            problem(adif_name, raw, _KIND_PROBLEM.get(kind, "is not valid"))
        elif value != "":
            fields[attr] = value

    # Start time: QSO_DATE + TIME_ON, both required by ADIF.
    on_date, on_time = _parse_date(rec.get("qso_date")), _parse_time(rec.get("time_on"))
    for name, parsed in (("qso_date", on_date), ("time_on", on_time)):
        if rec.get(name) and parsed is None:
            problem(name, rec[name], "is not a valid date/time")
    if on_date and on_time:
        fields["time_on"] = datetime.combine(on_date, on_time)

    # End time: QSO_DATE_OFF defaults to the start date; a TIME_OFF earlier than TIME_ON
    # without its own date means the QSO crossed midnight UTC.
    off_time = _parse_time(rec.get("time_off"))
    off_date = _parse_date(rec.get("qso_date_off"))
    for name, parsed in (("qso_date_off", off_date), ("time_off", off_time)):
        if rec.get(name) and parsed is None:
            problem(name, rec[name], "is not a valid date/time")
    if off_time and "time_on" in fields:
        end = datetime.combine(off_date or on_date, off_time)
        if off_date is None and end < fields["time_on"]:
            end += timedelta(days=1)
        fields["time_off"] = end
    elif off_time and off_date:
        fields["time_off"] = datetime.combine(off_date, off_time)
    else:
        # Valid end fields we cannot place in time (QSO_DATE_OFF without TIME_OFF, or no
        # usable start): keep them verbatim rather than lose them.
        for name in ("qso_date_off", "time_off"):
            if rec.get(name) and name not in extras:
                extras[name] = rec[name]

    for name, value in rec.items():
        if name not in mapped:
            extras[name] = value
    return fields, extras


def _is_derived_position(lat, lon, locator) -> bool:
    """True when (lat, lon) is just the centre of ``locator`` (filled in by enrich.py).

    Exporting it would invent precision the other logger never had, so we leave it out.
    """
    if lat is None or lon is None or not locator or not geo.is_valid_locator(locator):
        return False
    clat, clon = geo.locator_to_latlon(locator)
    return abs(clat - lat) < 1e-6 and abs(clon - lon) < 1e-6


def qso_to_record(qso) -> dict[str, str]:
    """Inverse of :func:`record_to_qso_fields`: a QSO as an ADIF record (lower-case names).

    Starts from the stored extras (``qso.extra_adif``); mapped fields override them. Adds
    ``app_shacklog_id`` and ``app_shacklog_tags`` so a re-import recognises the QSO.
    """
    extras: dict[str, str] = {}
    if getattr(qso, "extra_adif", None):
        try:
            loaded = json.loads(qso.extra_adif)
            if isinstance(loaded, dict):
                extras = {str(k).lower(): str(v) for k, v in loaded.items() if v is not None}
        except ValueError:
            pass  # a damaged blob must not block the export of the QSO itself

    rec: dict[str, str] = {}
    if qso.call:
        rec["call"] = qso.call
    if qso.time_on:
        rec["qso_date"] = qso.time_on.strftime("%Y%m%d")
        rec["time_on"] = qso.time_on.strftime("%H%M%S")
    if qso.time_off:
        rec["qso_date_off"] = qso.time_off.strftime("%Y%m%d")
        rec["time_off"] = qso.time_off.strftime("%H%M%S")

    derived_theirs = _is_derived_position(qso.latitude, qso.longitude, qso.locator)
    derived_mine = _is_derived_position(qso.my_latitude, qso.my_longitude, qso.my_locator)
    for adif_name, attr, kind in FIELD_MAP:
        value = getattr(qso, attr, None)
        if attr == "my_call" and not value:
            station = getattr(qso, "station", None)
            value = getattr(station, "callsign", None) if station is not None else None
        if value is None or value == "":
            continue
        if kind in ("lat", "lon"):
            if (attr in ("latitude", "longitude") and derived_theirs) or (
                    attr in ("my_latitude", "my_longitude") and derived_mine):
                continue
            rec[adif_name] = format_location(float(value), kind)
        elif kind in ("freq", "power"):
            rec[adif_name] = format_number(float(value))
        else:
            rec[adif_name] = str(value)

    # DISTANCE is not written from ``distance_km``: ShackLog recomputes it from positions,
    # and a written value would come back on re-import as a stale extra field. A DISTANCE
    # that came with an imported file is in the extras and goes out unchanged.
    if getattr(qso, "id", None):
        rec[APP_ID_FIELD] = qso.id

    # Extras fill in whatever the mapped fields didn't set (e.g. an invalid FREQ kept raw).
    for name in sorted(extras):
        if name not in rec:
            rec[name] = extras[name]
    return rec
