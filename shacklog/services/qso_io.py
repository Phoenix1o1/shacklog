"""ADIF import and export of QSOs, on top of the database model.

``services/adif.py`` knows the file format and the field mapping; this module adds what
needs the database: choosing the station a QSO was made from, filling derived fields
(``services/enrich.py``), and recognising QSOs that are already in the log.

Duplicates (CONVENTIONS.md §6)
------------------------------
A record is a duplicate when the log already holds a non-deleted QSO that

* has the id in the record's ``APP_SHACKLOG_ID`` (a ShackLog export coming back), or
* has the same call, band and display mode (``bands.display_mode``: ``DMR`` and
  ``DIGITALVOICE/DMR`` are the same thing) and a ``time_on`` within ±2 minutes.

Records repeated inside the same file are caught the same way. Duplicates are counted and
left alone: an import never overwrites what is in the log (MANDATES.md §3).

Speed: candidate QSOs are loaded with one query per batch of distinct callsigns (using the
``(call, time_on)`` index) and kept in an in-memory map, so 10 000 records cost a few
dozen queries instead of 10 000.

The caller commits. Nothing here commits, so a failed import can be rolled back whole.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from shacklog.models import Qso, Station
from shacklog.services import adif, bands, enrich

#: Two QSOs closer than this, same call/band/mode, are the same contact.
DUPLICATE_WINDOW = timedelta(minutes=2)
#: Records added and flushed together (keeps memory and flush time bounded).
FLUSH_EVERY = 500
#: Callsigns per ``IN (…)`` query; well below SQLite's variable limit.
_CALLS_PER_QUERY = 500


@dataclass
class ImportSummary:
    """What an import did. Shown on the "Import finished" page and stored in ImportLog."""

    created: int = 0
    duplicates: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    created_ids: list[str] = field(default_factory=list)


def summary_json(summary: ImportSummary) -> str:
    """The summary as JSON for ``ImportLog.summary``."""
    return json.dumps(asdict(summary), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Duplicate index
# ---------------------------------------------------------------------------

class _DuplicateIndex:
    """``(call, band, display mode) → [time_on, …]`` for the log plus this file so far."""

    def __init__(self) -> None:
        self._times: dict[tuple[str, str, str], list[datetime]] = {}
        self.ids: set[str] = set()

    @staticmethod
    def key(call: str, band: str | None, mode: str | None, submode: str | None) -> tuple[str, str, str]:
        return (call or "").upper(), (band or "").lower(), bands.display_mode(mode, submode)

    def add(self, call, band, mode, submode, time_on: datetime | None) -> None:
        if time_on is not None:
            self._times.setdefault(self.key(call, band, mode, submode), []).append(time_on)

    def contains(self, call, band, mode, submode, time_on: datetime) -> bool:
        # A handful of QSOs per (call, band, mode) at most, so a linear scan is fine.
        return any(abs(t - time_on) <= DUPLICATE_WINDOW
                   for t in self._times.get(self.key(call, band, mode, submode), ()))


def _load_existing(session: Session, calls: set[str], ids: set[str], index: _DuplicateIndex) -> None:
    """Put the log's non-deleted QSOs with these calls or ids into the duplicate index."""
    columns = (Qso.id, Qso.call, Qso.band, Qso.mode, Qso.submode, Qso.time_on)
    call_list = sorted(calls)
    for i in range(0, len(call_list), _CALLS_PER_QUERY):
        chunk = call_list[i:i + _CALLS_PER_QUERY]
        rows = session.execute(
            select(*columns).where(Qso.call.in_(chunk), Qso.deleted_at.is_(None))
        ).all()
        for row in rows:
            index.ids.add(row.id)
            index.add(row.call, row.band, row.mode, row.submode, row.time_on)
    id_list = sorted(ids - index.ids)
    for i in range(0, len(id_list), _CALLS_PER_QUERY):
        chunk = id_list[i:i + _CALLS_PER_QUERY]
        found = session.execute(
            select(Qso.id).where(Qso.id.in_(chunk), Qso.deleted_at.is_(None))
        ).scalars()
        index.ids.update(found)


# ---------------------------------------------------------------------------
# Station matching
# ---------------------------------------------------------------------------

def _station_matcher(session: Session):
    """Return ``match(my_call, my_locator) → station id | None`` over the live stations.

    A station matches when its callsign equals STATION_CALLSIGN and, when both the file and
    the station have a locator, the first four characters (the 1°×2° square) agree. That
    tells "IU2TST at home in Milan" from "IU2TST in Grenoble" without being fussy about
    the sub-square. When several stations fit, the oldest one wins (stable choice).
    """
    stations = (session.query(Station).filter(Station.deleted_at.is_(None))
                .order_by(Station.created_at, Station.id).all())
    by_call: dict[str, list[Station]] = {}
    for st in stations:
        if st.callsign:
            by_call.setdefault(st.callsign.replace(" ", "").upper(), []).append(st)
    cache: dict[tuple[str, str], str | None] = {}

    def match(my_call: str | None, my_locator: str | None) -> str | None:
        if not my_call:
            return None
        call = my_call.replace(" ", "").upper()
        square = (my_locator or "")[:4].upper()
        key = (call, square)
        if key not in cache:
            cache[key] = None
            for st in by_call.get(call, []):
                st_square = (st.locator or "")[:4].upper()
                if square and st_square and square != st_square:
                    continue
                cache[key] = st.id
                break
        return cache[key]

    return match


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _record_label(number: int, rec: dict[str, str]) -> str:
    call = rec.get("call")
    return f"Record {number} ({call.strip().upper()})" if call and call.strip() else f"Record {number}"


def import_adif(session: Session, data: bytes | str, station_id: str | None = None,
                filename: str | None = None) -> ImportSummary:
    """Import an ADI file into the log. Adds and flushes, never commits.

    ``station_id`` assigns every QSO to that station; otherwise each record is matched by
    STATION_CALLSIGN / MY_GRIDSQUARE (see :func:`_station_matcher`). ``filename`` is kept
    in ``Qso.import_ref`` so an import can be found (and undone) later.
    """
    summary = ImportSummary()
    station_id = station_id or None  # a blank form value means "match by file"
    try:
        _header, records = adif.parse_adi(data)
    except Exception as exc:  # pragma: no cover - parse_adi is meant to never raise
        summary.errors.append(f"The file could not be read as ADIF: {exc}")
        return summary
    if not records:
        summary.errors.append("No QSO records found. Is this an ADIF (.adi) file?")
        return summary

    match_station = _station_matcher(session) if station_id is None else None
    index = _DuplicateIndex()
    calls = {r["call"].replace(" ", "").upper() for r in records if r.get("call")}
    ids = {r[adif.APP_ID_FIELD].strip() for r in records if r.get(adif.APP_ID_FIELD)}
    _load_existing(session, calls, ids, index)
    import_ref = filename[:200] if filename else None

    pending = 0
    for number, rec in enumerate(records, start=1):
        label = _record_label(number, rec)
        problems: list[str] = []
        fields, extras = adif.record_to_qso_fields(rec, problems)
        if not fields.get("call"):
            summary.skipped += 1
            summary.errors.append(f"Record {number}: no CALL")
            continue
        if "time_on" not in fields:
            summary.skipped += 1
            what = "QSO_DATE/TIME_ON missing" if not (rec.get("qso_date") and rec.get("time_on")) \
                else "QSO_DATE/TIME_ON not a valid date and time"
            summary.errors.append(f"{label}: {what}")
            continue

        own_id = (extras.get(adif.APP_ID_FIELD) or "").strip()
        if own_id and own_id in index.ids:
            summary.duplicates += 1
            continue

        # Compare band and mode the way enrich.enrich_qso will store them ("DMR" equals
        # "DIGITALVOICE/DMR"; a missing BAND comes from FREQ). Doing it before enrichment
        # keeps a re-import of a big file (all duplicates) cheap. Imported records have no
        # repeater link, the only other input enrich uses for band and mode.
        band = bands.band_for_freq(fields.get("freq_mhz")) or fields.get("band")
        mode, submode = bands.normalize_mode(fields.get("mode"), fields.get("submode"))
        if index.contains(fields["call"], band, mode, submode, fields["time_on"]):
            summary.duplicates += 1
            continue

        qso = Qso(**fields)
        qso.extra_adif = json.dumps(extras, sort_keys=True, ensure_ascii=False) if extras else None
        qso.import_ref = import_ref
        qso.station_id = station_id or match_station(qso.my_call, qso.my_locator)
        enrich.enrich_qso(session, qso, is_new=True)

        for problem in problems:
            summary.errors.append(f"{label}: {problem}")
        session.add(qso)
        index.add(qso.call, qso.band, qso.mode, qso.submode, qso.time_on)
        if own_id:
            index.ids.add(own_id)
        index.ids.add(qso.id)
        summary.created += 1
        summary.created_ids.append(qso.id)
        pending += 1
        if pending >= FLUSH_EVERY:
            session.flush()
            pending = 0
    if pending:
        session.flush()
    return summary


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_adif(qsos: Iterable[Qso]) -> str:
    """The given QSOs as an ADI file (ShackLog header, one record per line)."""
    return adif.write_adi([adif.qso_to_record(q) for q in qsos])
