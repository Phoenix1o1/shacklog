"""Listening log (SWL/SDR): things heard without a two-way contact.

Same shape as the QSO section (see ``views/qsos.py``): ``FORM``, ``FILTERS``, ``SORTS``
and :func:`filtered_query`, which the CSV export and the map reuse so every view of the
log agrees. Derived fields (band, category guess, source position from the locator, my
position snapshot, distance) are filled by :func:`services.enrich.enrich_swl`.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from sqlalchemy import func

from shacklog import APP_NAME
from shacklog.models import SWL_CATEGORIES, Contact, Qso, SavedFilter, SwlEntry, utcnow
from shacklog.services import bands, enrich
from shacklog.services.links import links_for
from shacklog.web import filters as flt
from shacklog.web import forms
from shacklog.web import helpers as h
from shacklog.web.forms import Field, Section

bp = Blueprint("swl", __name__, url_prefix="/swl")
h.register_entity_view("swl", "swl.view")

#: Receivers: anything that can listen (an SDR, a transceiver, a handheld).
RECEIVER_KINDS = ("receiver", "radio", "handheld")

#: Suggestions for the mode box. Mode stays free text: listeners meet many more modes
#: than ADIF knows (APT, ACARS, POCSAG…), and old entries must survive an edit unchanged.
SWL_MODES = ["FM", "NFM", "WFM", "AM", "USB", "LSB", "CW", "DMR", "C4FM", "DSTAR", "M17",
             "FT8", "FT4", "RTTY", "PSK31", "SSTV", "APRS", "APT", "LRPT", "ADS-B", "AIS",
             "ACARS", "POCSAG", "DATA"]

#: Categories whose "heard" is a callsign, so it is upper-cased like a call.
CALLSIGN_CATEGORIES = ("amateur", "repeater")

FORM = [
    Section("Heard", [
        Field("time_utc", "Time (UTC)", type="datetime", required=True, width="third",
              attrs={"data-now": "1"}),
        Field("freq_mhz", "Frequency (MHz)", type="freq", width="third", placeholder="145.500",
              help="Empty: taken from the repeater."),
        Field("mode", "Mode", type="upper", width="third", placeholder="FM, AM, APT…",
              attrs={"list": "swl-modes"}),
        Field("category", "Category", type="select", choices=SWL_CATEGORIES, width="third",
              help="Leave empty to guess from the frequency."),
        Field("heard", "Heard", width="third",
              placeholder="Callsign, flight, satellite, station",
              attrs={"autocomplete": "off", "spellcheck": "false"}),
        Field("signal", "Signal", width="third", placeholder="S7, 59, SINPO 45444"),
        Field("description", "Description", width="full",
              placeholder="What was heard: net, ATIS, NOAA 19 pass…"),
    ]),
    Section("Receiving station", [
        Field("station_id", "Station", type="select", choices=h.station_choices, width="third"),
        Field("receiver_id", "Receiver", type="select",
              choices=h.equipment_choices(*RECEIVER_KINDS), width="third",
              help="Empty: the station's radio."),
        Field("antenna_id", "Antenna", type="select",
              choices=h.equipment_choices(*h.ANTENNA_KINDS), width="third"),
        Field("repeater_id", "Repeater", type="select", choices=h.repeater_choices, width="half"),
        Field("time_end", "End (UTC)", type="datetime", width="half"),
    ]),
    Section("Source position", [
        Field("locator", "Locator", type="locator", width="third", placeholder="JN45ol"),
        Field("latitude", "Latitude", type="lat", width="third"),
        Field("longitude", "Longitude", type="lon", width="third"),
    ], open=False, note="Where the signal came from. Coordinates are filled from the locator "
                        "or the repeater when left empty."),
    Section("Notes", [
        Field("notes", "Notes", type="textarea", width="full"),
        Field("tags", "Tags", type="tags", width="full", placeholder="sdr, noaa, contest"),
    ]),
]

#: Fields carried into the next form by "Log and start another".
STICKY = ("station_id", "freq_mhz", "mode", "category", "receiver_id", "antenna_id")

_FREQ_TEXT_RE = re.compile(r"^\s*(\d{1,9})(?:[.,](\d{1,6}))?\s*(ghz|mhz|khz|hz)?\s*$", re.I)
_UNIT_TO_MHZ = {"ghz": 1000.0, "mhz": 1.0, "khz": 0.001, "hz": 0.000001}


def parse_frequency_text(raw: str | None) -> float | None:
    """MHz from what a listener types, or None if the text isn't a frequency.

    ``forms.parse_freq`` reads any value outside the amateur bands as kHz, which is right
    for QSOs but wrong here: 137.1 (a NOAA satellite) or 118.7 (a tower) would become
    0.137 MHz. So for listening entries:

    * a unit wins: ``1090 MHz``, ``7074 kHz``, ``1.2 GHz``;
    * a decimal point or comma means MHz as typed: ``137.100``, ``145,65``;
    * a whole number above 3000 is kHz (``145500``, ``7074``): an RTL-SDR stops below
      2 GHz, so a bare "3500" means 80 m, not 3.5 GHz.
    """
    match = _FREQ_TEXT_RE.match(raw or "")
    if not match:
        return None
    whole, frac, unit = match.groups()
    value = float(f"{whole}.{frac}" if frac else whole)
    if unit:
        value *= _UNIT_TO_MHZ[unit.lower()]
    elif not frac and value > 3000:
        value /= 1000
    return round(value, 6) if value > 0 else None


def _mode_choices(s) -> list[tuple[str, str]]:
    """Modes actually present in the log (the filter only offers what can match)."""
    rows = (s.query(SwlEntry.mode).filter(SwlEntry.deleted_at.is_(None), SwlEntry.mode.isnot(None),
                                          SwlEntry.mode != "")
            .distinct().order_by(SwlEntry.mode).all())
    return [(m, m) for (m,) in rows]


FILTERS = [
    flt.Filter("heard", "Heard", "call", (SwlEntry.heard,), placeholder="IK2* or part"),
    flt.Filter("q", "Text", "text", (SwlEntry.heard, SwlEntry.description, SwlEntry.notes,
                                     SwlEntry.signal, SwlEntry.locator, SwlEntry.tags),
               placeholder="Description, notes…"),
    flt.Filter("category", "Category", "choice", (SwlEntry.category,), choices=SWL_CATEGORIES),
    flt.Filter("band", "Band", "choice", (SwlEntry.band,), choices=[(b, b) for b in bands.BAND_NAMES]),
    flt.Filter("mode", "Mode", "choice", (SwlEntry.mode,), choices=_mode_choices),
    flt.Filter("from", "From", "date_from", (SwlEntry.time_utc,)),
    flt.Filter("to", "To", "date_to", (SwlEntry.time_utc,)),
    flt.Filter("near", "Source near", "near", (SwlEntry.latitude, SwlEntry.longitude),
               choices=flt.station_center_choices),
    flt.Filter("receiver", "Receiver", "ref", (SwlEntry.receiver_id,),
               choices=h.equipment_choices(*RECEIVER_KINDS), advanced=True),
    flt.Filter("antenna", "Antenna", "ref", (SwlEntry.antenna_id,),
               choices=h.equipment_choices(*h.ANTENNA_KINDS), advanced=True),
    flt.Filter("repeater", "Repeater", "ref", (SwlEntry.repeater_id,), choices=h.repeater_choices,
               advanced=True),
    flt.Filter("station", "My station", "ref", (SwlEntry.station_id,), choices=h.station_choices,
               advanced=True),
    flt.Filter("fmin", "Frequency ≥ (MHz)", "min", (SwlEntry.freq_mhz,), advanced=True,
               placeholder="137"),
    flt.Filter("fmax", "Frequency ≤ (MHz)", "max", (SwlEntry.freq_mhz,), advanced=True,
               placeholder="138"),
    flt.Filter("tags", "Tags", "tags", (SwlEntry.tags,), advanced=True, placeholder="tag1, tag2"),
    flt.Filter("bbox", "Map area", "bbox", (SwlEntry.latitude, SwlEntry.longitude)),
]

SORTS = [
    flt.Sort("time", "Date", SwlEntry.time_utc, default_desc=True),
    flt.Sort("heard", "Heard", SwlEntry.heard),
    flt.Sort("freq", "Frequency", SwlEntry.freq_mhz),
    flt.Sort("category", "Category", SwlEntry.category),
    flt.Sort("dist", "Distance", SwlEntry.distance_km, default_desc=True),
    flt.Sort("distance", "Distance from point", None),
]


def filtered_query():
    """Listening entries matching the current request's filters (exports and map too)."""
    return flt.apply_filters(h.live(SwlEntry), FILTERS, request.args, h.session())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    s = h.session()
    result = filtered_query()
    query, sort_key, sort_dir = flt.apply_sort(result, SORTS, request.args, "time")
    if result.distance is not None:
        query = query.add_columns(result.distance.label("dist_from"))
    else:
        query = query.add_columns(SwlEntry.distance_km.label("dist_from"))
    page = h.paginate(query)
    saved = (s.query(SavedFilter).filter(SavedFilter.deleted_at.is_(None),
                                         SavedFilter.target == "swl")
             .order_by(SavedFilter.name).all())
    return render_template(
        "swl/index.html", page=page, filters=FILTERS,
        fchoices=flt.resolve_filter_choices(FILTERS, s), fresult=result,
        sort_key=sort_key, sort_dir=sort_dir, saved=saved, SWL_CATEGORIES=SWL_CATEGORIES,
    )


# ---------------------------------------------------------------------------
# Create / edit
# ---------------------------------------------------------------------------

def _parse_form() -> tuple[dict, dict, dict]:
    """``forms.parse`` plus the listening-log rules: own frequency reading, end ≥ start."""
    values, errors, raw = forms.parse(FORM, request.form)
    freq_text = (request.form.get("freq_mhz") or "").strip()
    errors.pop("freq_mhz", None)
    if freq_text:
        freq = parse_frequency_text(freq_text)
        if freq is None:
            errors["freq_mhz"] = "Enter a frequency in MHz, e.g. 137.100 (or 7074 kHz)"
        else:
            values["freq_mhz"] = freq
    else:
        values["freq_mhz"] = None
    start, end = values.get("time_utc"), values.get("time_end")
    if start and end and end < start:
        errors["time_end"] = "The end is before the start"
    return values, errors, raw


def _render_form(entry: SwlEntry | None, values, errors, title: str):
    return render_template(
        "swl/form.html", entry=entry, sections=FORM, values=values, errors=errors,
        choices=forms.resolve_choices(FORM, h.session()), title=title, modes=SWL_MODES,
    )


def _finish(s, entry: SwlEntry, typed_heard: str | None, is_new: bool, station_changed: bool) -> None:
    """Derived fields, then the category fallback and callsign casing.

    ``enrich_swl`` only guesses the category of *new* entries; an edit that empties the
    category gets the same guess here, and "other" when the frequency says nothing.
    """
    chosen = entry.category
    enrich.enrich_swl(s, entry, is_new=is_new, station_changed=station_changed)
    # enrich_swl treats "amateur" as "not chosen yet" (the column default) and may replace
    # it; in this form an empty select means "guess", so a picked category is kept.
    if chosen:
        entry.category = chosen
    if not entry.category:
        hint = bands.service_hint(entry.freq_mhz)
        entry.category = hint[0] if hint else "other"
    # Only what the user typed is upper-cased: a repeater *name* filled in by enrich
    # ("Milano R1 Campo dei Fiori") must keep its spelling.
    if typed_heard and entry.category in CALLSIGN_CATEGORIES:
        entry.heard = typed_heard.strip().upper()


@bp.route("/new", methods=["GET", "POST"])
def new():
    s = h.session()
    if request.method == "POST":
        values, errors, raw = _parse_form()
        if errors:
            return _render_form(None, raw, errors, "Log something heard"), 422
        entry = SwlEntry()
        forms.apply(entry, values)
        _finish(s, entry, values.get("heard"), is_new=True, station_changed=False)
        s.add(entry)
        s.commit()
        flash(f"Logged {entry.heard or entry.category}.", "ok")
        if request.form.get("again"):
            return redirect(url_for(".new", **{"from": entry.id}))
        return redirect(url_for(".view", record_id=entry.id))

    # GET: sticky fields from the previous entry, or the default station's setup.
    template = SwlEntry(time_utc=utcnow().replace(second=0, microsecond=0))
    template.category = None  # empty = "guess from the frequency"
    source = s.get(SwlEntry, request.args["from"]) if request.args.get("from") else None
    if source is not None and source.deleted_at is None:
        for name in STICKY:
            setattr(template, name, getattr(source, name))
    else:
        station = h.default_station(s)
        if station is not None:
            template.station_id = station.id
            template.receiver_id = station.radio_id
            template.antenna_id = station.antenna_id
    if request.args.get("repeater"):
        template.repeater_id = request.args["repeater"]
    if request.args.get("heard"):
        template.heard = request.args["heard"]
    if request.args.get("freq"):
        template.freq_mhz = parse_frequency_text(request.args["freq"])
    return _render_form(None, forms.values_from_obj(FORM, template), {}, "Log something heard")


@bp.route("/<record_id>/edit", methods=["GET", "POST"])
def edit(record_id: str):
    s = h.session()
    entry = h.get_or_404(SwlEntry, record_id)
    title = f"Edit {entry.heard or 'listening entry'}"
    if request.method == "POST":
        values, errors, raw = _parse_form()
        if errors:
            return _render_form(entry, raw, errors, title), 422
        old_station, old_freq = entry.station_id, entry.freq_mhz
        forms.apply(entry, values)
        if entry.freq_mhz != old_freq:
            entry.band = None  # recompute from the new frequency
        _finish(s, entry, values.get("heard"), is_new=False,
                station_changed=entry.station_id != old_station)
        s.commit()
        flash("Saved.", "ok")
        return redirect(url_for(".view", record_id=entry.id))
    return _render_form(entry, forms.values_from_obj(FORM, entry), {}, title)


@bp.route("/<record_id>/delete", methods=["POST"])
def delete(record_id: str):
    entry = h.get_or_404(SwlEntry, record_id)
    entry.soft_delete()
    h.session().commit()
    flash(f"{entry.heard or 'Listening entry'} moved to the trash.", "ok")
    return redirect(url_for(".index"))


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

@bp.route("/<record_id>")
def view(record_id: str):
    s = h.session()
    entry = h.get_or_404(SwlEntry, record_id)
    heard_key = (entry.heard or "").strip().upper()
    others, qsos, contact = [], [], None
    if heard_key:
        others = (h.live(SwlEntry).filter(func.upper(SwlEntry.heard) == heard_key,
                                          SwlEntry.id != entry.id)
                  .order_by(SwlEntry.time_utc.desc()).limit(20).all())
        if entry.category in CALLSIGN_CATEGORIES:
            call = heard_key.replace(" ", "")
            qsos = (h.live(Qso).filter(Qso.call == call).order_by(Qso.time_on.desc())
                    .limit(10).all())
            contact = h.live(Contact).filter(Contact.callsign == call).first()
    return render_template(
        "swl/view.html", entry=entry, others=others, qsos=qsos, contact=contact,
        callsign_like=entry.category in CALLSIGN_CATEGORIES and bool(heard_key),
        linked=links_for(s, "swl", entry.id), SWL_CATEGORIES=SWL_CATEGORIES,
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

#: Every stored field, in a readable order; references are exported by name after these.
CSV_COLUMNS = ["id", "time_utc", "time_end", "freq_mhz", "band", "mode", "category", "heard",
               "description", "signal", "locator", "latitude", "longitude", "my_latitude",
               "my_longitude", "distance_km", "bearing_deg", "notes", "tags", "created_at",
               "updated_at"]


@bp.route("/export.csv")
def export_csv():
    entries = filtered_query().query.order_by(SwlEntry.time_utc).all()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS + ["station", "receiver", "antenna", "repeater"])
    for e in entries:
        row = [getattr(e, c) for c in CSV_COLUMNS]
        row = [v.isoformat(sep=" ") if isinstance(v, datetime) else ("" if v is None else v)
               for v in row]
        row += [e.station.name if e.station else "", e.receiver.name if e.receiver else "",
                e.antenna.name if e.antenna else "",
                e.repeater.display_name() if e.repeater else ""]
        writer.writerow(row)
    name = f"{APP_NAME.lower()}-listening-{utcnow():%Y%m%d-%H%M}.csv"
    return Response(out.getvalue(), mimetype="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{name}"'})
