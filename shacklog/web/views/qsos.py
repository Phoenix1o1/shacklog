"""QSO log: list with filters, entry form, detail page, ADIF/CSV import and export.

This module is the **reference pattern** for every section (see PLAN.md §7):

* ``FORM``    — declarative form (web/forms.py)
* ``FILTERS`` — declarative filters (web/filters.py), shared by list, exports and map
* ``SORTS``   — sortable columns
* views: index · new · view · edit · delete · exports · imports · small htmx helpers
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import func, or_

from shacklog import APP_NAME
from shacklog.models import (
    PROP_MODES,
    QSL_STATUS,
    Contact,
    ImportLog,
    Qso,
    SavedFilter,
    SwlEntry,
    utcnow,
)
from shacklog.services import bands, enrich
from shacklog.services.links import links_for
from shacklog.web import filters as flt
from shacklog.web import forms
from shacklog.web import helpers as h
from shacklog.web.forms import Field, Section

bp = Blueprint("qsos", __name__, url_prefix="/qsos")
h.register_entity_view("qso", "qsos.view")

MODE_SELECT = [(m, m) for m in bands.MODE_CHOICES]

FORM = [
    Section("Contact", [
        Field("call", "Callsign", type="upper", required=True, width="third",
              attrs={"hx-get": "/qsos/check-call", "hx-trigger": "keyup changed delay:400ms, change",
                     "hx-target": "#call-hint", "autofocus": "autofocus"}),
        Field("time_on", "Start (UTC)", type="datetime", required=True, width="third",
              attrs={"data-now": "1"}),
        Field("freq_mhz", "Frequency (MHz)", type="freq", width="third",
              placeholder="145.500", help="Empty: taken from the repeater."),
        Field("mode", "Mode", type="select", choices=MODE_SELECT, width="quarter", blank=False),
        Field("rst_sent", "RST sent", width="quarter", placeholder="59"),
        Field("rst_rcvd", "RST received", width="quarter", placeholder="59"),
        Field("repeater_id", "Via repeater / node", type="select",
              choices=h.repeater_choices, width="quarter"),
        Field("name", "Name", width="third"),
        Field("qth", "QTH", width="third"),
        Field("locator", "Locator", type="locator", width="third", placeholder="JN45ol"),
    ]),
    Section("My station", [
        Field("station_id", "Station", type="select", choices=h.station_choices, width="third"),
        Field("radio_id", "Radio", type="select", choices=h.equipment_choices(*h.RADIO_KINDS),
              width="third", help="Empty: station default."),
        Field("antenna_id", "Antenna", type="select",
              choices=h.equipment_choices(*h.ANTENNA_KINDS), width="third"),
        Field("tx_power_w", "Power (W)", type="power", width="quarter"),
        Field("operator", "Operator", type="upper", width="quarter"),
        Field("time_off", "End (UTC)", type="datetime", width="half"),
    ]),
    Section("Path", [
        Field("prop_mode", "Propagation", type="select", choices=PROP_MODES, blank=False,
              width="third"),
        Field("freq_rx_mhz", "RX frequency (MHz)", type="freq", width="third"),
        Field("submode", "Submode", type="upper", width="third"),
        Field("sat_name", "Satellite", width="half", placeholder="ISS, SO-50…"),
        Field("sat_mode", "Satellite mode", width="half", placeholder="V/U"),
    ], open=False),
    Section("Their location", [
        Field("latitude", "Latitude", type="lat", width="quarter"),
        Field("longitude", "Longitude", type="lon", width="quarter"),
        Field("country", "Country", width="quarter"),
        Field("region", "Region / state", width="quarter"),
    ], open=False, note="Coordinates are filled from the locator when left empty."),
    Section("QSL & activities", [
        Field("qsl_sent", "QSL sent", type="select", choices=QSL_STATUS, blank=False, width="quarter"),
        Field("qsl_rcvd", "QSL received", type="select", choices=QSL_STATUS, blank=False, width="quarter"),
        Field("qsl_via", "QSL via", width="half"),
        Field("lotw_sent", "LoTW sent", type="select", choices=QSL_STATUS, blank=False, width="quarter"),
        Field("lotw_rcvd", "LoTW received", type="select", choices=QSL_STATUS, blank=False, width="quarter"),
        Field("eqsl_sent", "eQSL sent", type="select", choices=QSL_STATUS, blank=False, width="quarter"),
        Field("eqsl_rcvd", "eQSL received", type="select", choices=QSL_STATUS, blank=False, width="quarter"),
        Field("sota_ref", "Their SOTA", width="third"),
        Field("pota_ref", "Their POTA", width="third"),
        Field("wwff_ref", "Their WWFF", width="third"),
        Field("my_sota_ref", "My SOTA", width="third"),
        Field("my_pota_ref", "My POTA", width="third"),
        Field("my_wwff_ref", "My WWFF", width="third"),
        Field("contest_id", "Contest", width="half"),
    ], open=False),
    Section("Notes", [
        Field("comment", "Comment", width="full", help="Short; exported as ADIF COMMENT."),
        Field("notes", "Notes", type="textarea", width="full"),
        Field("tags", "Tags", type="tags", width="full", placeholder="portable, first contact"),
    ]),
]

#: Fields copied into a new form by "Log another" (HAMRS-style sticky fields).
STICKY = ("station_id", "radio_id", "antenna_id", "freq_mhz", "mode", "repeater_id",
          "tx_power_w", "operator", "prop_mode", "rst_sent", "rst_rcvd", "sat_name", "sat_mode")


def _mode_clause(value: str, _session):
    adif_mode, submode = bands.normalize_mode(value)
    if submode:
        return Qso.submode == submode
    return or_(Qso.mode == adif_mode, Qso.submode == adif_mode)


def _locator_prefix(value: str, _session):
    return Qso.locator.ilike(f"{value.strip()}%")


FILTERS = [
    flt.Filter("call", "Callsign", "call", (Qso.call,), placeholder="IK2* or part"),
    flt.Filter("q", "Text", "text", (Qso.name, Qso.qth, Qso.comment, Qso.notes, Qso.locator,
                                     Qso.tags, Qso.country, Qso.sat_name), placeholder="Name, QTH, notes…"),
    flt.Filter("band", "Band", "choice", (Qso.band,), choices=[(b, b) for b in bands.BAND_NAMES]),
    flt.Filter("mode", "Mode", "choice", custom=_mode_clause, choices=MODE_SELECT),
    flt.Filter("from", "From", "date_from", (Qso.time_on,)),
    flt.Filter("to", "To", "date_to", (Qso.time_on,)),
    flt.Filter("near", "Their station near", "near", (Qso.latitude, Qso.longitude),
               choices=flt.station_center_choices),
    flt.Filter("station", "My station", "ref", (Qso.station_id,), choices=h.station_choices, advanced=True),
    flt.Filter("radio", "Radio", "ref", (Qso.radio_id,), choices=h.equipment_choices(*h.RADIO_KINDS), advanced=True),
    flt.Filter("antenna", "Antenna", "ref", (Qso.antenna_id,), choices=h.equipment_choices(*h.ANTENNA_KINDS), advanced=True),
    flt.Filter("repeater", "Repeater", "ref", (Qso.repeater_id,), choices=h.repeater_choices, advanced=True),
    flt.Filter("prop", "Propagation", "choice", (Qso.prop_mode,), choices=PROP_MODES[1:], advanced=True),
    flt.Filter("locator", "Locator starts with", "text", custom=_locator_prefix, advanced=True, placeholder="JN45"),
    flt.Filter("country", "Country", "text", (Qso.country,), advanced=True),
    flt.Filter("dmin", "Distance ≥ (km)", "min", (Qso.distance_km,), advanced=True),
    flt.Filter("dmax", "Distance ≤ (km)", "max", (Qso.distance_km,), advanced=True),
    flt.Filter("from_near", "Operated from near", "near", (Qso.my_latitude, Qso.my_longitude),
               choices=flt.station_center_choices, advanced=True),
    flt.Filter("qsl", "QSL received", "choice", (Qso.qsl_rcvd,), choices=QSL_STATUS[1:], advanced=True),
    flt.Filter("tags", "Tags", "tags", (Qso.tags,), advanced=True, placeholder="tag1, tag2"),
    flt.Filter("bbox", "Map area", "bbox", (Qso.latitude, Qso.longitude)),
]

SORTS = [
    flt.Sort("time", "Date", Qso.time_on, default_desc=True),
    flt.Sort("call", "Call", Qso.call),
    flt.Sort("freq", "Frequency", Qso.freq_mhz),
    flt.Sort("band", "Band", Qso.band),
    flt.Sort("mode", "Mode", Qso.mode),
    flt.Sort("dist", "Distance", Qso.distance_km, default_desc=True),
    flt.Sort("distance", "Distance from point", None),
]


def filtered_query():
    """QSOs matching the current request's filters (also used by exports and the map)."""
    s = h.session()
    query = h.live(Qso)
    return flt.apply_filters(query, FILTERS, request.args, s)


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
        query = query.add_columns(Qso.distance_km.label("dist_from"))
    page = h.paginate(query)
    saved = (s.query(SavedFilter).filter(SavedFilter.deleted_at.is_(None),
                                         SavedFilter.target == "qso")
             .order_by(SavedFilter.name).all())
    return render_template(
        "qsos/index.html", page=page, filters=FILTERS,
        fchoices=flt.resolve_filter_choices(FILTERS, s), fresult=result,
        sort_key=sort_key, sort_dir=sort_dir, saved=saved,
    )


# ---------------------------------------------------------------------------
# Create / edit
# ---------------------------------------------------------------------------

def _form_values_for(qso: Qso) -> dict[str, str]:
    values = forms.values_from_obj(FORM, qso)
    values["mode"] = bands.display_mode(qso.mode, qso.submode)
    if qso.submode and qso.submode == values["mode"]:
        values["submode"] = ""  # already expressed by the mode selector
    return values


def _render_form(qso: Qso | None, values, errors, title: str):
    return render_template(
        "qsos/form.html", qso=qso, sections=FORM, values=values, errors=errors,
        choices=forms.resolve_choices(FORM, h.session()), title=title,
    )


@bp.route("/new", methods=["GET", "POST"])
def new():
    s = h.session()
    if request.method == "POST":
        values, errors, raw = forms.parse(FORM, request.form)
        if errors:
            return _render_form(None, raw, errors, "Log a QSO"), 422
        qso = Qso()
        forms.apply(qso, values)
        enrich.enrich_qso(s, qso, is_new=True)
        s.add(qso)
        s.commit()
        flash(f"Logged {qso.call}.", "ok")
        if request.form.get("again"):
            return redirect(url_for(".new", **{"from": qso.id}))
        return redirect(url_for(".view", record_id=qso.id))

    # GET: start from a previous QSO's sticky fields, or the station defaults.
    template = Qso(time_on=utcnow().replace(second=0, microsecond=0), mode="FM",
                   prop_mode="", rst_sent="59", rst_rcvd="59")
    source = s.get(Qso, request.args.get("from", "")) if request.args.get("from") else None
    if source is not None:
        for name in STICKY:
            setattr(template, name, getattr(source, name))
    else:
        station = h.default_station(s)
        if station is not None:
            template.station_id = station.id
            template.radio_id = station.radio_id
            template.antenna_id = station.antenna_id
            template.tx_power_w = station.power_w
    if request.args.get("call"):
        template.call = request.args["call"].upper()
    if request.args.get("repeater"):
        template.repeater_id = request.args["repeater"]
    values = _form_values_for(template)
    return _render_form(None, values, {}, "Log a QSO")


@bp.route("/<record_id>/edit", methods=["GET", "POST"])
def edit(record_id: str):
    s = h.session()
    qso = h.get_or_404(Qso, record_id)
    if request.method == "POST":
        values, errors, raw = forms.parse(FORM, request.form)
        if errors:
            return _render_form(qso, raw, errors, f"Edit {qso.call}"), 422
        old_station, old_freq = qso.station_id, qso.freq_mhz
        forms.apply(qso, values)
        if qso.freq_mhz != old_freq:
            qso.band = None  # recompute from the new frequency
        enrich.enrich_qso(s, qso, is_new=False, station_changed=qso.station_id != old_station)
        s.commit()
        flash("Saved.", "ok")
        return redirect(url_for(".view", record_id=qso.id))
    return _render_form(qso, _form_values_for(qso), {}, f"Edit {qso.call}")


@bp.route("/<record_id>/delete", methods=["POST"])
def delete(record_id: str):
    qso = h.get_or_404(Qso, record_id)
    qso.soft_delete()
    h.session().commit()
    flash(f"{qso.call} moved to the trash.", "ok")
    return redirect(url_for(".index"))


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

@bp.route("/<record_id>")
def view(record_id: str):
    s = h.session()
    qso = h.get_or_404(Qso, record_id)
    others = (h.live(Qso).filter(Qso.call == qso.call, Qso.id != qso.id)
              .order_by(Qso.time_on.desc()).limit(20).all())
    contact = h.live(Contact).filter(Contact.callsign == qso.call).first()
    return render_template("qsos/view.html", qso=qso, others=others, contact=contact,
                           linked=links_for(s, "qso", qso.id), PROP_MODES=PROP_MODES,
                           QSL_STATUS=QSL_STATUS)


@bp.route("/check-call")
def check_call():
    """htmx fragment under the callsign box: have I worked or heard this station?"""
    call = (request.args.get("call") or "").replace(" ", "").upper()
    if len(call) < 3:
        return ""
    q = h.live(Qso).filter(Qso.call == call)
    count = q.count()
    last = q.order_by(Qso.time_on.desc()).first()
    heard = h.live(SwlEntry).filter(func.upper(SwlEntry.heard) == call).count()
    contact = h.live(Contact).filter(Contact.callsign == call).first()
    country = h.country_of(call)
    new_country = bool(country) and not h.live(Qso).filter(
        Qso.country == country["entity"].name).count()
    book = h.callbook()
    return render_template("qsos/_call_hint.html", call=call, count=count, last=last,
                           heard=heard, contact=contact, country=country,
                           new_country=new_country, book=book, entry=book.cached(call))


@bp.route("/callbook")
def callbook_lookup():
    """htmx fragment: ask the callbook about a callsign (only when the user presses
    *Look up*, so typing doesn't spend lookups)."""
    from shacklog.services.callbook import CallbookError

    call = (request.args.get("call") or "").replace(" ", "").upper()
    book = h.callbook()
    error = None
    entry = None
    try:
        entry = book.lookup(call, refresh=bool(request.args.get("refresh")))
    except CallbookError as exc:
        error = str(exc)
    return render_template("qsos/_callbook.html", call=call, book=book, entry=entry,
                           error=error, looked_up=True)


# ---------------------------------------------------------------------------
# Export / import
# ---------------------------------------------------------------------------

def _export_name(ext: str) -> str:
    return f"{APP_NAME.lower()}-qsos-{datetime.utcnow():%Y%m%d-%H%M}.{ext}"


@bp.route("/export.adi")
def export_adi():
    from shacklog.services import qso_io

    result = filtered_query()
    qsos = result.query.order_by(Qso.time_on).all()
    text = qso_io.export_adif(qsos)
    return Response(text, mimetype="text/plain; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{_export_name("adi")}"'})


CSV_COLUMNS = ["time_on", "time_off", "call", "name", "qth", "locator", "country",
               "freq_mhz", "freq_rx_mhz", "band", "mode", "submode", "rst_sent", "rst_rcvd",
               "tx_power_w", "my_call", "my_locator", "prop_mode", "sat_name", "distance_km",
               "bearing_deg", "qsl_sent", "qsl_rcvd", "comment", "notes", "tags"]


@bp.route("/export.csv")
def export_csv():
    result = filtered_query()
    qsos = result.query.order_by(Qso.time_on).all()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS + ["station", "radio", "antenna", "repeater"])
    for q in qsos:
        row = [getattr(q, c) for c in CSV_COLUMNS]
        row = [v.isoformat(sep=" ") if isinstance(v, datetime) else ("" if v is None else v)
               for v in row]
        row += [q.station.name if q.station else "", q.radio.name if q.radio else "",
                q.antenna.name if q.antenna else "",
                q.repeater.display_name() if q.repeater else ""]
        writer.writerow(row)
    return Response(out.getvalue(), mimetype="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{_export_name("csv")}"'})


@bp.route("/import", methods=["GET", "POST"])
def import_adif():
    from shacklog.services import backup, qso_io

    s = h.session()
    if request.method == "POST":
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            flash("Choose an ADIF file (.adi) first.", "error")
            return redirect(url_for(".import_adif"))
        backup.create_backup(h.database(), current_app.config["PATHS"].backups,
                             reason="before-adif-import")
        data = upload.read()
        summary = qso_io.import_adif(s, data, station_id=request.form.get("station_id") or None,
                                     filename=upload.filename)
        s.add(ImportLog(kind="adif", filename=upload.filename, summary=qso_io.summary_json(summary)))
        s.commit()
        return render_template("qsos/import_done.html", summary=summary, filename=upload.filename)
    return render_template("qsos/import.html", stations=h.station_choices(s),
                           default_station=h.default_station(s))
