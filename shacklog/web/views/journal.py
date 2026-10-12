"""Antenna & test journal and station diary.

A journal entry (``kind`` "test" or "diary") has free text, optional *measurement rows*
(SWR, power, return loss, impedance at a frequency) edited inline in the entry form, and
NanoVNA sweeps attached as library files (``.s1p``/``.s2p`` or category "sweep"). The
detail page parses each sweep with ``services.touchstone`` and draws the SWR curve.

Structure follows ``views/qsos.py`` (FORM / FILTERS / SORTS / filtered_query).
"""

from __future__ import annotations

import importlib
import math
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from flask import Blueprint, flash, redirect, render_template, request, url_for
from markupsafe import Markup
from sqlalchemy import and_, exists, func, or_, select

from shacklog.models import (
    JOURNAL_KINDS,
    JournalEntry,
    Link,
    Measurement,
    SavedFilter,
    StoredFile,
    utcnow,
)
from shacklog.services.links import links_for
from shacklog.web import filters as flt
from shacklog.web import forms
from shacklog.web import helpers as h
from shacklog.web.forms import Field, FieldError, Section
from shacklog.web.views.swl import parse_frequency_text

bp = Blueprint("journal", __name__, url_prefix="/journal")
h.register_entity_view("journal", "journal.view")

FORM = [
    Section("Entry", [
        Field("kind", "Kind", type="select", choices=JOURNAL_KINDS, blank=False, width="third"),
        Field("time_utc", "Date & time (UTC)", type="datetime", required=True, width="third",
              attrs={"data-now": "1"}),
        Field("equipment_id", "Equipment under test", type="select",
              choices=h.equipment_choices(), width="third"),
        Field("title", "Title", required=True, width="full",
              placeholder="Dual-band J-pole, first sweep after trimming"),
        Field("body", "Notes", type="textarea", width="full", attrs={"rows": 8},
              help="Blank lines start a new paragraph; links become clickable."),
    ]),
    Section("Where & tags", [
        Field("station_id", "Station", type="select", choices=h.station_choices, width="half"),
        Field("tags", "Tags", type="tags", width="half", placeholder="j-pole, 2m, coax"),
    ]),
]

#: Columns of a measurement row, in table order: (field, label, kind).
MEASURE_FIELDS: list[tuple[str, str, str]] = [
    ("freq_mhz", "Frequency (MHz)", "freq"),
    ("swr", "SWR", "swr"),
    ("power_fwd_w", "Forward (W)", "power"),
    ("power_ref_w", "Reflected (W)", "power"),
    ("return_loss_db", "Return loss (dB)", "rl"),
    ("r_ohm", "R (Ω)", "r"),
    ("x_ohm", "X (Ω)", "x"),
    ("note", "Note", "text"),
]
MEASURE_NAMES = [name for name, _, _ in MEASURE_FIELDS]
#: Blank rows offered under the existing ones.
BLANK_ROWS = 3

#: Bands shaded on sweep plots (Andrea's dual-band antennas): only drawn when the sweep
#: covers them, clipped to its range.
SHADE_BANDS_MHZ: list[tuple[float, float]] = [(144.0, 146.0), (430.0, 440.0)]
SWEEP_EXTENSIONS = (".s1p", ".s2p")
#: Largest sweep file we try to parse (a 10 001-point .s2p is about 1.5 MB).
MAX_SWEEP_BYTES = 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def _is_sweep_file():
    """SQL: a live library file that is a VNA sweep (category or extension)."""
    name = func.lower(StoredFile.filename)
    return and_(StoredFile.deleted_at.is_(None),
                or_(StoredFile.category == "sweep",
                    *[name.like(f"%{ext}") for ext in SWEEP_EXTENSIONS]))


def _has_sweep_clause():
    """EXISTS a live link, in either direction, between the entry and a sweep file."""
    forward = (select(Link.id).join(StoredFile, StoredFile.id == Link.b_id)
               .where(Link.deleted_at.is_(None), Link.a_type == "journal",
                      Link.a_id == JournalEntry.id, Link.b_type == "file", _is_sweep_file()))
    backward = (select(Link.id).join(StoredFile, StoredFile.id == Link.a_id)
                .where(Link.deleted_at.is_(None), Link.b_type == "journal",
                       Link.b_id == JournalEntry.id, Link.a_type == "file", _is_sweep_file()))
    return or_(exists(forward), exists(backward))


def _has_sweep(value: str, _session):
    clause = _has_sweep_clause()
    return clause if value == "yes" else ~clause


FILTERS = [
    flt.Filter("kind", "Kind", "choice", (JournalEntry.kind,), choices=JOURNAL_KINDS),
    flt.Filter("q", "Text", "text", (JournalEntry.title, JournalEntry.body, JournalEntry.tags),
               placeholder="Title, notes…"),
    flt.Filter("equipment", "Equipment", "ref", (JournalEntry.equipment_id,),
               choices=h.equipment_choices()),
    flt.Filter("sweep", "Has a sweep", "has", custom=_has_sweep),
    flt.Filter("from", "From", "date_from", (JournalEntry.time_utc,), advanced=True),
    flt.Filter("to", "To", "date_to", (JournalEntry.time_utc,), advanced=True),
    flt.Filter("station", "Station", "ref", (JournalEntry.station_id,), choices=h.station_choices,
               advanced=True),
    flt.Filter("tags", "Tags", "tags", (JournalEntry.tags,), advanced=True, placeholder="tag1, tag2"),
]

SORTS = [
    flt.Sort("time", "Date", JournalEntry.time_utc, default_desc=True),
    flt.Sort("title", "Title", JournalEntry.title),
    flt.Sort("kind", "Kind", JournalEntry.kind),
]


def filtered_query():
    """Journal entries matching the current request's filters."""
    return flt.apply_filters(h.live(JournalEntry), FILTERS, request.args, h.session())


# ---------------------------------------------------------------------------
# SWR arithmetic
# ---------------------------------------------------------------------------

def swr_from_power(fwd_w: float | None, ref_w: float | None) -> float | None:
    """SWR = (1 + √(Pr/Pf)) / (1 − √(Pr/Pf)); None when unknown or not finite (Pr ≥ Pf)."""
    if fwd_w is None or ref_w is None or fwd_w <= 0 or ref_w < 0:
        return None
    rho = math.sqrt(ref_w / fwd_w)
    if rho >= 1:
        return None
    return (1 + rho) / (1 - rho)


@dataclass
class Row:
    """A measurement prepared for display: stored values plus the SWR to show."""

    m: Measurement
    swr: float | None
    computed: bool


def display_rows(measurements: list[Measurement]) -> list[Row]:
    rows = []
    for m in measurements:
        if m.swr is not None:
            rows.append(Row(m, m.swr, False))
        else:
            calc = swr_from_power(m.power_fwd_w, m.power_ref_w)
            rows.append(Row(m, calc, calc is not None))
    return rows


def _measurements(entry_id: str) -> list[Measurement]:
    return (h.live(Measurement).filter(Measurement.entry_id == entry_id)
            .order_by(Measurement.freq_mhz.is_(None), Measurement.freq_mhz,
                      Measurement.created_at).all())


# ---------------------------------------------------------------------------
# Measurement rows in the form
# ---------------------------------------------------------------------------

def _row_values(m: Measurement | None) -> dict[str, str]:
    out = {"id": m.id if m else ""}
    for name, _label, kind in MEASURE_FIELDS:
        value = getattr(m, name) if m else None
        if value is None:
            out[name] = ""
        elif kind == "freq":
            out[name] = forms.format_value(Field(name, name, type="freq"), value)
        elif isinstance(value, float):
            out[name] = f"{value:g}"
        else:
            out[name] = str(value)
    return out


def _form_rows(entry: JournalEntry | None) -> list[dict[str, str]]:
    existing = _measurements(entry.id) if entry is not None else []
    return [_row_values(m) for m in existing] + [_row_values(None) for _ in range(BLANK_ROWS)]


def _parse_cell(kind: str, raw: str) -> Any:
    if kind == "text":
        return raw[:300]
    if kind == "freq":
        value = parse_frequency_text(raw)
        if value is None:
            raise FieldError("MHz, e.g. 145.500")
        return value
    try:
        value = float(raw.replace(" ", "").replace(",", "."))
    except ValueError:
        raise FieldError("Enter a number") from None
    if not math.isfinite(value):
        raise FieldError("Enter a number")
    if kind == "swr" and value < 1:
        raise FieldError("SWR is 1 or more")
    if kind in ("power", "r") and value < 0:
        raise FieldError("Can't be negative")
    if kind == "rl":
        # NanoVNA shows S11 as a negative dB figure; return loss is the same number, positive.
        value = abs(value)
    return value


def parse_rows(formdata) -> tuple[list[dict], dict[tuple[int, str], str], list[dict[str, str]]]:
    """Read the measurement grid.

    Returns ``(rows, errors, raw)``: per row ``{"id", "blank", "values"}``, errors keyed by
    (row index, field), and the raw strings to redisplay. Every row submits every input,
    so the lists of each column line up.
    """
    columns = {name: formdata.getlist(f"m_{name}") for name in MEASURE_NAMES}
    ids = formdata.getlist("m_id")
    count = max([len(ids)] + [len(v) for v in columns.values()])
    rows, errors, raw_rows = [], {}, []
    for i in range(count):
        raw = {"id": ids[i] if i < len(ids) else ""}
        values: dict[str, Any] = {}
        for name, _label, kind in MEASURE_FIELDS:
            text = (columns[name][i] if i < len(columns[name]) else "").strip()
            raw[name] = text
            if text == "":
                values[name] = None
                continue
            try:
                values[name] = _parse_cell(kind, text)
            except FieldError as exc:
                errors[(i, name)] = str(exc)
        blank = all(raw[name] == "" for name in MEASURE_NAMES)
        rows.append({"id": raw["id"], "blank": blank, "values": values})
        raw_rows.append(raw)
    return rows, errors, raw_rows


def save_rows(s, entry: JournalEntry, rows: list[dict]) -> tuple[int, int, int]:
    """Apply parsed rows: create, update, or soft-delete (a cleared row). Returns counts."""
    existing = {m.id: m for m in _measurements(entry.id)}
    added = changed = removed = 0
    for row in rows:
        current = existing.get(row["id"]) if row["id"] else None
        if row["blank"]:
            if current is not None:
                current.soft_delete()
                removed += 1
            continue
        if current is None:
            m = Measurement(entry_id=entry.id)
            forms.apply(m, row["values"])
            s.add(m)
            added += 1
        else:
            before = [getattr(current, n) for n in MEASURE_NAMES]
            forms.apply(current, row["values"])
            if before != [getattr(current, n) for n in MEASURE_NAMES]:
                changed += 1
    return added, changed, removed


# ---------------------------------------------------------------------------
# Sweeps
# ---------------------------------------------------------------------------

def _touchstone():
    """The Touchstone service, or None when this build lacks it (lazy: owned elsewhere)."""
    try:
        return importlib.import_module("shacklog.services.touchstone")
    except ModuleNotFoundError as exc:
        if exc.name == "shacklog.services.touchstone":
            return None
        raise


def is_sweep(f: StoredFile) -> bool:
    return f.category == "sweep" or (f.filename or "").lower().endswith(SWEEP_EXTENSIONS)


def sweep_files(entry: JournalEntry) -> list[StoredFile]:
    """Library files linked to the entry that are VNA sweeps, oldest link first."""
    seen, out = set(), []
    for item in links_for(h.session(), "journal", entry.id):
        f = item.obj
        if item.entity == "file" and f is not None and f.id not in seen and is_sweep(f):
            seen.add(f.id)
            out.append(f)
    return out


def _read_text(sha256: str) -> str:
    store = h.filestore()
    with store.open(sha256) as fh:
        data = fh.read(MAX_SWEEP_BYTES + 1)
    if len(data) > MAX_SWEEP_BYTES:
        raise ValueError("file too large for a sweep")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def shade_for(summary: dict) -> list[tuple[float, float]]:
    """Ham bands to shade, clipped to the sweep range (empty if the sweep misses them)."""
    start, stop = summary.get("start_mhz"), summary.get("stop_mhz")
    if start is None or stop is None:
        return []
    return [(max(lo, start), min(hi, stop)) for lo, hi in SHADE_BANDS_MHZ if lo < stop and hi > start]


@dataclass
class Sweep:
    file: StoredFile
    state: str  # "ok" | "missing" | "error" | "unavailable"
    summary: dict = field(default_factory=dict)
    svg: Markup | None = None
    error: str | None = None
    shade: list[tuple[float, float]] = field(default_factory=list)


def analyse_sweep(f: StoredFile, ts, with_svg: bool = True) -> Sweep:
    """Parse one sweep file. Never raises: problems become a state shown on the page."""
    if not h.filestore().exists(f.sha256):
        return Sweep(f, "missing")
    if ts is None:
        return Sweep(f, "unavailable")
    try:
        parsed = ts.parse_touchstone(_read_text(f.sha256))
        summary = dict(ts.summarize(parsed) or {})
        shade = shade_for(summary)
        svg = None
        if with_svg:
            # swr_svg builds the SVG itself from numbers (trusted contract), so it is
            # inserted as markup.
            svg = Markup(ts.swr_svg(parsed, shade=shade or None))
        return Sweep(f, "ok", summary, svg, shade=shade)
    except Exception as exc:  # malformed file, unsupported variant, empty data…
        return Sweep(f, "error", error=str(exc) or exc.__class__.__name__)


#: sha256 → summary. Blobs never change, so a summary computed once stays right; this
#: keeps the list page from re-parsing every sweep on every view.
_SUMMARY_CACHE: "OrderedDict[tuple[str, int], dict | None]" = OrderedDict()
_SUMMARY_CACHE_SIZE = 512


def cached_min_swr(f: StoredFile, ts) -> float | None:
    if ts is None or not h.filestore().exists(f.sha256):
        return None
    key = (f.sha256, id(ts))
    if key not in _SUMMARY_CACHE:
        sweep = analyse_sweep(f, ts, with_svg=False)
        _SUMMARY_CACHE[key] = sweep.summary if sweep.state == "ok" else None
        while len(_SUMMARY_CACHE) > _SUMMARY_CACHE_SIZE:
            _SUMMARY_CACHE.popitem(last=False)
    summary = _SUMMARY_CACHE[key]
    return summary.get("min_swr") if summary else None


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    s = h.session()
    result = filtered_query()
    query, sort_key, sort_dir = flt.apply_sort(result, SORTS, request.args, "time")
    page = h.paginate(query)
    # Lowest SWR per entry on this page: measured/computed rows, else the sweeps.
    ids = [e.id for e in page.items]
    by_entry: dict[str, list[Measurement]] = {}
    if ids:
        for m in h.live(Measurement).filter(Measurement.entry_id.in_(ids)).all():
            by_entry.setdefault(m.entry_id, []).append(m)
    ts = None
    min_swr: dict[str, tuple[float, str]] = {}
    sweeps: dict[str, int] = {}
    for e in page.items:
        values = [r.swr for r in display_rows(by_entry.get(e.id, [])) if r.swr is not None]
        files = sweep_files(e)
        sweeps[e.id] = len(files)
        if values:
            min_swr[e.id] = (min(values), "measured")
        elif files:
            if ts is None:
                ts = _touchstone()
            swept = [v for v in (cached_min_swr(f, ts) for f in files) if v is not None]
            if swept:
                min_swr[e.id] = (min(swept), "sweep")
    saved = (s.query(SavedFilter).filter(SavedFilter.deleted_at.is_(None),
                                         SavedFilter.target == "journal")
             .order_by(SavedFilter.name).all())
    return render_template(
        "journal/index.html", page=page, filters=FILTERS,
        fchoices=flt.resolve_filter_choices(FILTERS, s), fresult=result,
        sort_key=sort_key, sort_dir=sort_dir, saved=saved, min_swr=min_swr, sweeps=sweeps,
        JOURNAL_KINDS=JOURNAL_KINDS,
    )


# ---------------------------------------------------------------------------
# Create / edit
# ---------------------------------------------------------------------------

def _render_form(entry, values, errors, rows, row_errors, title: str):
    return render_template(
        "journal/form.html", entry=entry, sections=FORM, values=values, errors=errors,
        choices=forms.resolve_choices(FORM, h.session()), title=title, rows=rows,
        row_errors=row_errors, measure_fields=MEASURE_FIELDS,
        rows_open=values.get("kind") == "test" or any(
            any(r.get(n) for n in MEASURE_NAMES) for r in rows),
    )


def _row_flash(added: int, changed: int, removed: int) -> str:
    bits = []
    if added:
        bits.append(f"{added} measurement{'s' if added != 1 else ''} added")
    if changed:
        bits.append(f"{changed} changed")
    if removed:
        bits.append(f"{removed} removed")
    return (" " + ", ".join(bits).capitalize() + ".") if bits else ""


@bp.route("/new", methods=["GET", "POST"])
def new():
    s = h.session()
    if request.method == "POST":
        values, errors, raw = forms.parse(FORM, request.form)
        rows, row_errors, raw_rows = parse_rows(request.form)
        if errors or row_errors:
            return _render_form(None, raw, errors, raw_rows, row_errors, "New journal entry"), 422
        entry = JournalEntry()
        forms.apply(entry, values)
        s.add(entry)
        s.flush()
        counts = save_rows(s, entry, rows)
        s.commit()
        flash(f"Saved “{entry.title}”.{_row_flash(*counts)}", "ok")
        return redirect(url_for(".view", record_id=entry.id))

    kind = request.args.get("kind")
    template = JournalEntry(kind=kind if kind in dict(JOURNAL_KINDS) else "diary",
                            time_utc=utcnow().replace(second=0, microsecond=0))
    station = h.default_station(s)
    if station is not None:
        template.station_id = station.id
    if request.args.get("equipment"):
        template.equipment_id = request.args["equipment"]
        if not kind:
            template.kind = "test"
    values = forms.values_from_obj(FORM, template)
    return _render_form(None, values, {}, _form_rows(None), {}, "New journal entry")


@bp.route("/<record_id>/edit", methods=["GET", "POST"])
def edit(record_id: str):
    s = h.session()
    entry = h.get_or_404(JournalEntry, record_id)
    title = f"Edit “{entry.title}”"
    if request.method == "POST":
        values, errors, raw = forms.parse(FORM, request.form)
        rows, row_errors, raw_rows = parse_rows(request.form)
        if errors or row_errors:
            return _render_form(entry, raw, errors, raw_rows, row_errors, title), 422
        forms.apply(entry, values)
        counts = save_rows(s, entry, rows)
        s.commit()
        flash(f"Saved.{_row_flash(*counts)}", "ok")
        return redirect(url_for(".view", record_id=entry.id))
    return _render_form(entry, forms.values_from_obj(FORM, entry), {}, _form_rows(entry), {}, title)


@bp.route("/<record_id>/delete", methods=["POST"])
def delete(record_id: str):
    entry = h.get_or_404(JournalEntry, record_id)
    # Measurement rows stay as they are: they are only reachable through the entry, and
    # restoring the entry from the trash then brings them back intact.
    entry.soft_delete()
    h.session().commit()
    flash(f"“{entry.title}” moved to the trash.", "ok")
    return redirect(url_for(".index"))


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

@bp.route("/<record_id>")
def view(record_id: str):
    s = h.session()
    entry = h.get_or_404(JournalEntry, record_id)
    ts = None
    sweeps = []
    files = sweep_files(entry)
    if files:
        ts = _touchstone()
        sweeps = [analyse_sweep(f, ts) for f in files]
    return render_template(
        "journal/view.html", entry=entry, rows=display_rows(_measurements(entry.id)),
        sweeps=sweeps, linked=links_for(s, "journal", entry.id), JOURNAL_KINDS=JOURNAL_KINDS,
        measure_fields=MEASURE_FIELDS,
    )
