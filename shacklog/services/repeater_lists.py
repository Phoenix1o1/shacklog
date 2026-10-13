"""Repeater lists from other sources → repeater records.

Repeater lists come in many shapes: RepeaterBook's JSON API and CSV exports, national
lists such as IK2ANE's for Italy, spreadsheets passed around by clubs. Rather than one
parser per list, this module reads any table (CSV with ``,`` ``;`` or tab, or RepeaterBook
JSON) and **guesses which column holds what** from the header names, in English,
Italian and French. The guess is shown before importing and can be corrected (the web
page passes a ``mapping``), so a new list format needs no new code.

Values are then cleaned up the way such lists write them: ``145,6125``, ``-600`` kHz or
``-0.6`` MHz shifts, ``88.5 Hz``, ``D023`` DCS codes, ``CC1``, ``DMR/C4FM``, locators,
``45.47N``-style coordinates.

Pure logic: no Flask, no database. :func:`read_table` → :func:`guess_mapping` (headers, rows) →
:func:`rows_to_repeaters`.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field

from shacklog.services import geo

#: The repeater fields a column can be mapped to, with the header names that suggest them
#: (compared after lower-casing and removing everything but letters and digits).
FIELDS: dict[str, tuple[str, list[str]]] = {
    "output_mhz": ("Output frequency (MHz)", [
        "frequency", "freq", "output", "outputfreq", "outputfrequency", "downlink",
        "frequenza", "frequenzauscita", "uscita", "freqout", "fout", "qrg", "frequence",
        "frequencesortie", "sortie"]),
    # (TX/RX columns are left alone: lists disagree on whose TX they mean.)
    "input_mhz": ("Input frequency (MHz)", [
        "inputfreq", "inputfrequency", "input", "uplink", "ingresso",
        "frequenzaingresso", "freqin", "fin", "entree", "frequenceentree"]),
    "shift": ("Shift / offset", [
        "offset", "shift", "duplex", "passo", "scostamento", "decalage", "split"]),
    "ctcss_up": ("Tone to send (CTCSS, Hz)", [
        "uplinktone", "pl", "ctcss", "tone", "tono", "subtono", "subtone", "ctcssup",
        "toneup", "accesstone", "ctcssaccesso", "tonalita", "squelchtone", "rtone"]),
    "ctcss_down": ("Tone the repeater sends (Hz)", [
        "downlinktone", "tsq", "ctcssdown", "tonedown", "ctone", "ctcssuscita"]),
    "dcs_code": ("DCS code", ["dcs", "dcscode", "dtcs", "dtcscode"]),
    "callsign": ("Callsign", [
        "call", "callsign", "nominativo", "indicativo", "indicatif"]),
    "name": ("Name", ["name", "nome", "nom", "repeater", "ponte", "denominazione", "relais"]),
    "ident": ("Ident (R1, RU5…)", ["ident", "canale", "channel", "ch", "id", "sigla"]),
    "kind": ("Mode", [
        "mode", "modo", "modalita", "type", "tipo", "system", "digitalmode"]),
    "color_code": ("DMR colour code", ["colorcode", "colourcode", "cc", "dmrcolorcode"]),
    "talkgroups": ("Talkgroups", ["talkgroups", "tg", "talkgroup", "tgs"]),
    "network": ("Network", [
        "network", "rete", "reseau", "dmrnetwork", "linked"]),
    "echolink_node": ("EchoLink node", ["echolink", "echolinknode", "nodo"]),
    "location_name": ("Location", [
        "location", "nearestcity", "city", "landmark", "localita", "qth", "sito", "site",
        "luogo", "comune", "ville", "lieu", "emplacement", "postazione"]),
    "region": ("Region / state", [
        "region", "regione", "state", "departement", "dept", "provincia", "province",
        "prov"]),  # earlier = preferred when a list has several of these columns
    "country": ("Country", ["country", "nazione", "paese", "pays", "stato"]),
    "locator": ("Locator", ["locator", "grid", "gridsquare", "locatore", "qthlocator", "loc",
                            "maidenhead", "wwlocator"]),
    "latitude": ("Latitude", ["lat", "latitude", "latitudine"]),
    "longitude": ("Longitude", ["long", "lon", "lng", "longitude", "longitudine"]),
    "altitude_m": ("Altitude (m)", ["altitude", "altitudine", "alt", "asl", "elevation", "quota",
                                    "slm", "altezza"]),
    "status": ("Status", ["status", "operationalstatus", "opstatus", "statooperativo", "statut",
                          "attivo"]),
    "last_verified": ("Last checked", ["lastupdate", "lastedited", "updated", "verified",
                                       "aggiornato", "aggiornamento", "data"]),
    "notes": ("Notes", ["notes", "note", "comment", "comments", "remarks", "commento",
                        "commenti", "remarques", "info"]),
}
IGNORE = ""  # mapping value for "don't import this column"

# RepeaterBook marks each digital mode in a column of its own ("Yes"/"No").
_RB_MODE_COLUMNS = [("DMR", "DMR"), ("D-Star", "DSTAR"), ("System Fusion", "C4FM"),
                    ("M17", "M17"), ("FM Analog", "FM")]


@dataclass
class Table:
    """A list as read from the file: header names and rows of text."""

    headers: list[str]
    rows: list[list[str]]
    source: str = "CSV"  # "CSV" or "RepeaterBook JSON"


@dataclass
class Result:
    repeaters: list[dict] = field(default_factory=list)  # Repeater field values
    errors: list[str] = field(default_factory=list)


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower()
                  .translate(str.maketrans("àáâäèéêëìíîïòóôöùúûü", "aaaaeeeeiiiioooouuuu")))


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------

def decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_table(text: str) -> Table:
    """Read RepeaterBook JSON or a delimited text table. Raises ValueError if neither."""
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        return _read_json(stripped)
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError("The file needs a header line and at least one repeater.")
    # The header is the first line that names a frequency column (lists often start with
    # a title or a date line); the delimiter is the one that finds the widest such line.
    best: tuple[int, int, list[list[str]]] | None = None  # (columns, header index, rows)
    for delimiter in (",", ";", "\t", "|"):
        rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
        for index, row in enumerate(rows[:10]):
            if {_key(c) for c in row} & set(FIELDS["output_mhz"][1]):
                if best is None or len(row) > best[0]:
                    best = (len(row), index, rows)
                break
    if best is None:
        raise ValueError("No column named like a frequency (Frequency, Output, Frequenza…) "
                         "in the first lines.")
    _, start, reader = best
    headers = [h.strip() for h in reader[start]]
    rows = [r for r in reader[start + 1:] if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("No repeaters below the header line.")
    return Table(headers, rows)


def _read_json(text: str) -> Table:
    data = json.loads(text)
    results = data.get("results") if isinstance(data, dict) else data
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise ValueError("A JSON file must be RepeaterBook's: {\"results\": [{…}, …]}.")
    headers: list[str] = []
    for item in results:
        for key in item:
            if key not in headers:
                headers.append(key)
    rows = [["" if item.get(h) is None else str(item.get(h)) for h in headers]
            for item in results]
    return Table(headers, rows, source="RepeaterBook JSON")


# ---------------------------------------------------------------------------
# Which column is what
# ---------------------------------------------------------------------------

def guess_mapping(headers: list[str], rows: list[list[str]] | None = None) -> dict[int, str]:
    """``{column index: field}`` for the columns whose name says what they hold.

    An exact synonym wins over a partial one, and each field is used once (the first
    column that fits): "Frequency" and "Input Freq" don't both become the output.
    Columns empty in every row (given ``rows``) are left out, so RepeaterBook's empty
    "Region" doesn't hide its "State".
    """
    keys = [_key(h) for h in headers]
    if rows is not None:
        keys = [k if any(i < len(r) and r[i].strip() for r in rows) else ""
                for i, k in enumerate(keys)]
    mapping: dict[int, str] = {}
    # Exact names first; when several columns fit a field, the earlier synonym wins.
    for name, (_label, synonyms) in FIELDS.items():
        candidates = [(synonyms.index(key), index) for index, key in enumerate(keys)
                      if key in synonyms and index not in mapping]
        if candidates:
            mapping[min(candidates)[1]] = name
    # Then partial names ("Frequenza uscita MHz") for the fields still free.
    used = set(mapping.values())
    for index, key in enumerate(keys):
        if index in mapping or not key:
            continue
        for name, (_label, synonyms) in FIELDS.items():
            if name not in used and any(len(s) > 3 and s in key for s in synonyms):
                mapping[index] = name
                used.add(name)
                break
    return mapping


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

_NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


def number(text: str | None) -> float | None:
    """First number in the text; a decimal comma is fine (``145,6125``)."""
    match = _NUMBER.search(text or "")
    return float(match.group(0).replace(",", ".")) if match else None


def frequency_mhz(text: str | None) -> float | None:
    """MHz from ``145.6125``, ``145,6125 MHz``, ``430 900`` or kHz/Hz values."""
    text = text or ""
    if re.fullmatch(r"\s*\d+ \d+\s*", text):  # "430 900": digits grouped by a space
        text = text.replace(" ", "")
    value = number(text)
    if value is None or value <= 0:
        return None
    if value > 1e6:  # Hz
        value /= 1e6
    elif value > 1e4:  # kHz
        value /= 1e3
    return round(value, 6)


def shift_mhz(text: str | None, output: float | None) -> float | None:
    """Signed shift in MHz: ``-0.6``, ``-600`` (kHz), ``+7.6 MHz``, ``-``/``+`` alone
    (the usual shift of the band), or an input frequency written in the shift column."""
    raw = (text or "").strip().lower()
    if not raw or raw in ("0", "simplex", "off", "none"):
        return 0.0 if raw else None
    if raw in ("-", "+", "minus", "plus"):
        default = _default_shift(output)
        if default is None:
            return None
        return -default if raw in ("-", "minus") else default
    value = number(raw)
    if value is None:
        return None
    if output and abs(value) > 25 and abs(value - output) < 50 and "khz" not in raw:
        return round(value - output, 6)  # an input frequency, not a shift
    if "khz" in raw or abs(value) >= 20:
        value /= 1000
    if raw.startswith("-") or "minus" in raw or "neg" in raw:
        value = -abs(value)
    return round(value, 6)


def _default_shift(output: float | None) -> float | None:
    """The common shift of the band (IARU region 1 / US): 2 m 0.6, 70 cm 7.6 or 5,
    10 m 0.1, 6 m 0.6, 23 cm 6/28."""
    if output is None:
        return None
    if 28 <= output < 30:
        return 0.1
    if 50 <= output < 54 or 144 <= output < 148:
        return 0.6
    if 420 <= output < 450:
        return 7.6 if output < 440 else 5.0
    if 1240 <= output < 1300:
        return 28.0 if output > 1290 else 6.0
    return None


def tone(text: str | None) -> tuple[float | None, str | None]:
    """(CTCSS Hz, DCS code) from ``88.5``, ``88,5 Hz``, ``CTCSS 123``, ``D023``, ``023N``."""
    raw = (text or "").strip().upper()
    if not raw or raw in ("NO", "NONE", "CSQ", "-", "0", "OFF"):
        return None, None
    dcs = re.fullmatch(r"D?(\d{3})[NI]?", raw.replace("DCS", "").strip())
    if dcs and (raw.startswith("D") or raw.endswith(("N", "I")) or "DCS" in raw):
        return None, dcs.group(1)
    value = number(raw)
    if value is not None and 60 <= value <= 260:
        return round(value, 1), None
    return None, None


def mode(text: str | None) -> str | None:
    """REPEATER_KINDS value from free text: the first digital mode named, else FM."""
    raw = _key(text or "")
    if not raw:
        return None
    for needle, kind in (("dmr", "DMR"), ("c4fm", "C4FM"), ("fusion", "C4FM"), ("ysf", "C4FM"),
                         ("wiresx", "C4FM"), ("dstar", "DSTAR"), ("m17", "M17"),
                         ("echolink", "ECHOLINK"), ("allstar", "ALLSTAR"), ("aprs", "APRS"),
                         ("beacon", "BEACON"), ("faro", "BEACON"), ("fm", "FM"),
                         ("analog", "FM"), ("nfm", "FM"), ("ponte", "FM")):
        if needle in raw:
            return kind
    return None


def coordinate(text: str | None) -> float | None:
    """Decimal degrees from ``45.47``, ``45,47``, ``45.47N``, ``9.18 E``, ``W 71.1``."""
    raw = (text or "").strip().upper()
    value = number(raw)
    if value is None:
        return None
    if re.search(r"[SW]", raw.replace("SW", "")) and value > 0:
        value = -value
    return value


def _status(text: str | None) -> str | None:
    raw = _key(text or "")
    if not raw:
        return None
    if raw in ("onair", "active", "attivo", "yes", "si", "ok", "on", "1", "operational",
               "actif", "enservice"):
        return "active"
    if raw in ("offair", "inactive", "off", "no", "spento", "disattivo", "0", "nonattivo",
               "horsservice", "down"):
        return "inactive"
    return "unknown"


def _date(text: str | None):
    from datetime import date

    raw = (text or "").strip()
    for pattern, order in ((r"(\d{4})-(\d{1,2})-(\d{1,2})", "ymd"),
                           (r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", "dmy")):
        match = re.search(pattern, raw)
        if match:
            a, b, c = (int(x) for x in match.groups())
            y, m, d = (a, b, c) if order == "ymd" else (c, b, a)
            try:
                return date(y, m, d)
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------
# Rows → repeaters
# ---------------------------------------------------------------------------

def _yes(text: str | None) -> bool:
    return _key(text or "") in ("yes", "y", "true", "1", "si")


def rows_to_repeaters(table: Table, mapping: dict[int, str]) -> Result:
    """Repeater field values for every row that has an output frequency."""
    result = Result()
    headers_by_key = {_key(h): i for i, h in enumerate(table.headers)}
    for number_, row in enumerate(table.rows, start=1):
        cells = {name: (row[i].strip() if i < len(row) else "")
                 for i, name in mapping.items() if name}
        try:
            fields = _row_fields(cells, row, headers_by_key)
        except Exception as exc:  # one odd row must not stop the list
            result.errors.append(f"Row {number_}: {exc}")
            continue
        if fields is None:
            result.errors.append(f"Row {number_}: no output frequency, skipped.")
            continue
        result.repeaters.append(fields)
    return result


def _row_fields(cells: dict[str, str], row: list[str], headers_by_key: dict[str, int]) -> dict | None:
    output = frequency_mhz(cells.get("output_mhz"))
    if output is None:
        return None
    fields: dict = {"output_mhz": output}
    if cells.get("input_mhz"):
        fields["input_mhz"] = frequency_mhz(cells["input_mhz"])
    elif cells.get("shift"):
        shift = shift_mhz(cells["shift"], output)
        if shift is not None:
            fields["input_mhz"] = round(output + shift, 6)
    up_tone, dcs = tone(cells.get("ctcss_up"))
    fields["ctcss_up"] = up_tone
    fields["ctcss_down"] = tone(cells.get("ctcss_down"))[0]
    fields["dcs_code"] = tone("D" + cells["dcs_code"].lstrip("Dd"))[1] if cells.get("dcs_code") else dcs

    for name in ("callsign", "ident", "talkgroups", "network", "echolink_node",
                 "location_name", "region", "country", "notes"):
        if cells.get(name):
            fields[name] = cells[name]
    if fields.get("callsign"):
        fields["callsign"] = fields["callsign"].replace(" ", "").upper()
    if cells.get("color_code"):
        cc = number(cells["color_code"])
        fields["color_code"] = int(cc) if cc is not None and 0 <= cc <= 15 else None

    kind = mode(cells.get("kind"))
    if kind is None:  # RepeaterBook: one Yes/No column per mode, digital ones first
        for column, value in _RB_MODE_COLUMNS:
            index = headers_by_key.get(_key(column))
            if index is not None and index < len(row) and _yes(row[index]):
                kind = value
                break
    fields["kind"] = kind or "FM"
    if fields["kind"] == "DMR" and fields.get("color_code") is None:
        found = re.search(r"\bCC\s*(\d{1,2})\b", " ".join(cells.values()), re.I)
        if found and int(found.group(1)) <= 15:
            fields["color_code"] = int(found.group(1))

    lat, lon = coordinate(cells.get("latitude")), coordinate(cells.get("longitude"))
    if lat is not None and lon is not None and (lat, lon) != (0, 0) \
            and -90 <= lat <= 90 and -180 <= lon <= 180:
        fields["latitude"], fields["longitude"] = lat, lon
    if cells.get("locator"):
        fields["locator"] = geo.normalize_locator(cells["locator"]) or None
    if cells.get("altitude_m"):
        fields["altitude_m"] = number(cells["altitude_m"])
    if cells.get("status"):
        fields["status"] = _status(cells["status"]) or "unknown"
    if cells.get("last_verified"):
        fields["last_verified"] = _date(cells["last_verified"])

    name = cells.get("name") or cells.get("location_name") or ""
    call = fields.get("callsign")
    if call and name and call.upper() not in name.upper():
        name = f"{call} {name}"
    fields["name"] = (name or call or f"{output:.4f}")[:120]
    return {k: v for k, v in fields.items() if v not in (None, "")}
