"""Repeaters: list with filters (band, distance from a station…), detail, CHIRP import/export.

The repeater list is where Andrea builds radio programming: filter "within 60 km of
Grenoble", order by distance, and export the result as a CHIRP CSV ready to paste into
the UV-K1 or the TM-733E. A CHIRP CSV from someone else (or from a radio) can be imported
to grow the list; exact duplicates are skipped.

Every repeater page shows what links to it: QSOs and listening entries made through it,
radio channels programmed with it, and how far it is from each of my stations.

CHIRP parsing and writing live in ``services/chirp.py`` (imported lazily so the app boots
without it).
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime

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
from sqlalchemy import func, null, or_

from shacklog import APP_NAME
from shacklog.models import (
    REPEATER_KINDS,
    REPEATER_STATUS,
    ImportLog,
    Qso,
    RadioChannel,
    RadioConfig,
    Repeater,
    SavedFilter,
    Station,
    SwlEntry,
    utcnow,
)
from shacklog.services import bands, geo
from shacklog.services.links import links_for
from shacklog.web import filters as flt
from shacklog.web import forms
from shacklog.web import helpers as h
from shacklog.web.forms import Field, Section
from shacklog.web.views.stations import (
    check_coordinates,
    fill_position,
    reference_station,
    station_position,
)

bp = Blueprint("repeaters", __name__, url_prefix="/repeaters")
h.register_entity_view("repeater", "repeaters.view")

#: Kinds an analog FM radio (UV-K1, TM-733E) can use. C4FM repeaters are included because
#: most Italian/French Fusion repeaters run in AMS mode and also pass analog FM; EchoLink
#: and AllStar entries here are RF nodes reached in FM.
FM_KINDS = ("FM", "C4FM", "ECHOLINK", "ALLSTAR")
FM_KINDS_LABEL = "FM, C4FM (AMS), EchoLink and AllStar"

#: Bands where repeaters live, for the band filter.
REPEATER_BANDS = ["10m", "6m", "4m", "2m", "1.25m", "70cm", "23cm", "13cm"]

FORM = [
    Section("Identity", [
        Field("name", "Name", required=True, width="half", placeholder="Milano R1 Campo dei Fiori"),
        Field("callsign", "Callsign", type="upper", width="quarter", placeholder="IR2UFD"),
        Field("ident", "Channel / ident", width="quarter", placeholder="R1, RU5…"),
        Field("kind", "Kind", type="select", choices=REPEATER_KINDS, blank=False, width="third"),
        Field("network", "Network", width="third", placeholder="BrandMeister, WIRES-X…"),
        Field("status", "Status", type="select", choices=REPEATER_STATUS, blank=False, width="third"),
    ]),
    Section("Frequencies & access", [
        Field("output_mhz", "Output (MHz)", type="freq", width="third", placeholder="145.6000",
              help="What I listen on."),
        Field("input_mhz", "Input (MHz)", type="freq", width="third", placeholder="145.0000",
              help="What I transmit on."),
        Field("shift", "Shift (MHz)", type="float", width="third", placeholder="-0.6",
              help="Only used when the input is empty: −0.6 on 2 m, +1.6 or −7.6 on 70 cm."),
        Field("ctcss_up", "CTCSS to send (Hz)", type="float", width="quarter", placeholder="88.5"),
        Field("ctcss_down", "CTCSS it sends (Hz)", type="float", width="quarter"),
        Field("dcs_code", "DCS", width="quarter", placeholder="023"),
        Field("color_code", "DMR colour code", type="int", width="quarter"),
        Field("talkgroups", "Talkgroups", width="half", placeholder="TG222, TG2231…"),
        Field("echolink_node", "EchoLink node", width="half"),
    ]),
    Section("Location", [
        Field("location_name", "Site", width="half", placeholder="Monte Penice"),
        Field("locator", "Locator", type="locator", width="quarter", placeholder="JN44ns"),
        Field("altitude_m", "Altitude (m)", type="float", width="quarter"),
        Field("latitude", "Latitude", type="lat", width="quarter"),
        Field("longitude", "Longitude", type="lon", width="quarter"),
        Field("country", "Country", width="quarter", placeholder="Italy"),
        Field("region", "Region", width="quarter", placeholder="Lombardia"),
    ], note="Give a locator or coordinates; the other is computed when you save."),
    Section("Source & notes", [
        Field("source", "Source", width="half", placeholder="ARI list 2026, on air…"),
        Field("last_verified", "Last verified", type="date", width="half"),
        Field("notes", "Notes", type="textarea", width="full"),
        Field("tags", "Tags", type="tags", width="full", placeholder="linked, emergency"),
    ]),
]


def _distinct_choices(column):
    def choices(s):
        rows = (s.query(column).filter(Repeater.deleted_at.is_(None), column.isnot(None),
                                       column != "")
                .distinct().order_by(column).all())
        return [(r[0], r[0]) for r in rows]

    return choices


def _call_or_ident(value: str, _session):
    text = value.strip().upper().replace(" ", "")
    if "*" in text or "?" in text:
        pattern = text.replace("*", "%").replace("?", "_")
    else:
        pattern = f"%{text}%"
    return or_(func.upper(Repeater.callsign).like(pattern), func.upper(Repeater.ident).like(pattern))


def _band_clause(value: str, _session):
    edges = bands.band_range(value)
    if edges is None:
        raise ValueError(value)
    return Repeater.output_mhz.between(*edges)


def _has_column(column):
    def clause(value: str, _session):
        return column.isnot(None) if value == "yes" else column.is_(None)

    return clause


FILTERS = [
    flt.Filter("q", "Text", "text", (Repeater.name, Repeater.callsign, Repeater.ident,
                                     Repeater.location_name, Repeater.region, Repeater.network,
                                     Repeater.talkgroups, Repeater.notes, Repeater.source),
               placeholder="Name, site, network, notes…"),
    flt.Filter("call", "Callsign or ident", "call", custom=_call_or_ident, placeholder="IR2* or R1"),
    flt.Filter("band", "Band", "choice", custom=_band_clause,
               choices=[(b, b) for b in REPEATER_BANDS]),
    flt.Filter("near", "Near", "near", (Repeater.latitude, Repeater.longitude),
               choices=flt.station_center_choices),
    flt.Filter("kind", "Kind", "choice", (Repeater.kind,), choices=REPEATER_KINDS, advanced=True),
    flt.Filter("status", "Status", "choice", (Repeater.status,), choices=REPEATER_STATUS, advanced=True),
    flt.Filter("country", "Country", "choice", (Repeater.country,),
               choices=_distinct_choices(Repeater.country), advanced=True),
    flt.Filter("region", "Region", "choice", (Repeater.region,),
               choices=_distinct_choices(Repeater.region), advanced=True),
    flt.Filter("fmin", "Output ≥ (MHz)", "min", (Repeater.output_mhz,), advanced=True),
    flt.Filter("fmax", "Output ≤ (MHz)", "max", (Repeater.output_mhz,), advanced=True),
    flt.Filter("ctcss", "Has CTCSS", "has", custom=_has_column(Repeater.ctcss_up), advanced=True),
    flt.Filter("located", "Has position", "has", custom=_has_column(Repeater.latitude), advanced=True),
    flt.Filter("tags", "Tags", "tags", (Repeater.tags,), advanced=True, placeholder="tag1, tag2"),
    flt.Filter("bbox", "Map area", "bbox", (Repeater.latitude, Repeater.longitude)),
]

SORTS = [
    flt.Sort("name", "Name", func.lower(Repeater.name)),
    flt.Sort("output", "Output", Repeater.output_mhz),
    flt.Sort("country", "Country", Repeater.country),
    flt.Sort("distance", "Distance", None),
]


def filtered_query():
    """Repeaters matching the current request's filters (also used by exports and the map)."""
    return flt.apply_filters(h.live(Repeater), FILTERS, request.args, h.session())


def _with_reference(result: flt.FilterResult) -> flt.FilterResult:
    """Without a ``near`` centre, measure distances from the reference station.

    Only adds a distance column (no filtering), so the list can show and sort by
    "km from home" without the user typing anything.
    """
    if result.distance is None:
        ref = reference_station(h.session())
        pos = station_position(ref)
        if pos is not None:
            result.distance = func.geo_dist_km(Repeater.latitude, Repeater.longitude, *pos)
            result.center, result.center_label = pos, ref.name
    return result


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

#: Query parameters of the CHIRP export form (never treated as filters).
EXPORT_OPTIONS = ("start", "order", "fm_only", "max", "name_len", "opts")


@bp.route("/")
def index():
    s = h.session()
    result = _with_reference(filtered_query())
    default_sort = "distance" if result.distance is not None else "output"
    query, sort_key, sort_dir = flt.apply_sort(result, SORTS, request.args, default_sort)
    query = query.order_by(Repeater.output_mhz)  # stable secondary order
    dist = result.distance.label("dist") if result.distance is not None else null().label("dist")
    page = h.paginate(query.add_columns(dist))
    saved = (s.query(SavedFilter).filter(SavedFilter.deleted_at.is_(None),
                                         SavedFilter.target == "repeater")
             .order_by(SavedFilter.name).all())
    carried = [(k, v) for k, v in request.args.items(multi=True)
               if k not in ("page", "sort", "dir") + EXPORT_OPTIONS and v != ""]
    return render_template(
        "repeaters/index.html", page=page, filters=FILTERS,
        fchoices=flt.resolve_filter_choices(FILTERS, s), fresult=result,
        sort_key=sort_key, sort_dir=sort_dir, saved=saved, carried=carried,
        fm_kinds_label=FM_KINDS_LABEL,
    )


# ---------------------------------------------------------------------------
# Create / edit / delete
# ---------------------------------------------------------------------------

def _render_form(rep: Repeater | None, values, errors, title: str):
    return render_template(
        "repeaters/form.html", rep=rep, sections=FORM, values=values, errors=errors,
        choices=forms.resolve_choices(FORM, h.session()), title=title,
    )


def _parse():
    """Parse the form; turn the helper "shift" field into an input frequency."""
    values, errors, raw = forms.parse(FORM, request.form)
    check_coordinates(values, errors)
    shift = values.pop("shift", None)
    if values.get("input_mhz") is None and shift is not None:
        if values.get("output_mhz") is None:
            errors.setdefault("output_mhz", "Give the output frequency to apply a shift.")
        else:
            values["input_mhz"] = round(values["output_mhz"] + shift, 6)
    return values, errors, raw


@bp.route("/new", methods=["GET", "POST"])
def new():
    s = h.session()
    if request.method == "POST":
        values, errors, raw = _parse()
        if errors:
            return _render_form(None, raw, errors, "New repeater"), 422
        rep = Repeater()
        forms.apply(rep, values)
        fill_position(rep)
        s.add(rep)
        s.commit()
        flash(f"Added repeater {rep.name}.", "ok")
        return redirect(url_for(".view", record_id=rep.id))
    template = Repeater(kind="FM", status="active")
    ref = reference_station(s)
    if ref is not None:
        template.country, template.region = ref.country, ref.region
    return _render_form(None, forms.values_from_obj(FORM, template), {}, "New repeater")


@bp.route("/<record_id>/edit", methods=["GET", "POST"])
def edit(record_id: str):
    s = h.session()
    rep = h.get_or_404(Repeater, record_id)
    if request.method == "POST":
        values, errors, raw = _parse()
        if errors:
            return _render_form(rep, raw, errors, f"Edit {rep.name}"), 422
        before = (rep.locator, rep.latitude, rep.longitude)
        forms.apply(rep, values)
        fill_position(rep, before)
        s.commit()
        flash("Saved.", "ok")
        return redirect(url_for(".view", record_id=rep.id))
    return _render_form(rep, forms.values_from_obj(FORM, rep), {}, f"Edit {rep.name}")


@bp.route("/<record_id>/delete", methods=["POST"])
def delete(record_id: str):
    rep = h.get_or_404(Repeater, record_id)
    rep.soft_delete()
    h.session().commit()
    flash(f"{rep.name} moved to the trash. QSOs made through it keep the link.", "ok")
    return redirect(url_for(".index"))


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

@bp.route("/<record_id>")
def view(record_id: str):
    s = h.session()
    rep = h.get_or_404(Repeater, record_id)
    qsos = h.live(Qso).filter(Qso.repeater_id == rep.id)
    swl = h.live(SwlEntry).filter(SwlEntry.repeater_id == rep.id)
    channels = (s.query(RadioChannel, RadioConfig)
                .join(RadioConfig, RadioChannel.config_id == RadioConfig.id)
                .filter(RadioChannel.deleted_at.is_(None), RadioConfig.deleted_at.is_(None),
                        RadioChannel.repeater_id == rep.id)
                .order_by(RadioConfig.is_current.desc(), RadioConfig.config_date.desc(),
                          RadioChannel.location)
                .all())
    from_stations = []
    if rep.has_position:
        for st in h.live(Station).order_by(Station.kind, Station.name):
            pos = station_position(st)
            if pos is None:
                continue
            from_stations.append((st, geo.haversine_km(pos[0], pos[1], rep.latitude, rep.longitude),
                                  geo.bearing_deg(pos[0], pos[1], rep.latitude, rep.longitude)))
    return render_template(
        "repeaters/view.html", rep=rep, band=bands.band_for_freq(rep.output_mhz),
        qso_count=qsos.count(), latest_qsos=qsos.order_by(Qso.time_on.desc()).limit(20).all(),
        swl_count=swl.count(), latest_swl=swl.order_by(SwlEntry.time_utc.desc()).limit(10).all(),
        channels=channels, from_stations=from_stations,
        linked=links_for(s, "repeater", rep.id),
    )


# ---------------------------------------------------------------------------
# CHIRP import
# ---------------------------------------------------------------------------

#: Columns of Repeater that an import may set (no bookkeeping columns).
_REPEATER_FIELDS = {c for c in Repeater.__table__.columns.keys()} - {
    "id", "created_at", "updated_at", "deleted_at", "origin_instance",
    "modified_by_instance", "change_seq", "received_from"}

_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{1,3}[0-9][A-Z0-9]{0,3}[A-Z]$")
_LOCATOR_IN_TEXT = re.compile(r"\b([A-R]{2}[0-9]{2}[A-X]{2})\b", re.IGNORECASE)


def decode_upload(data: bytes) -> str:
    """CSV bytes → text. CHIRP writes UTF-8; older Windows exports may be cp1252."""
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def is_simplex(ch: dict) -> bool:
    """A channel without a repeater shift (simplex, or receive-only with duplex "off")."""
    return (ch.get("duplex") or "").strip().lower() in ("", "off")


def repeater_fields_from_channel(chirp, ch: dict) -> dict:
    """Repeater values for a CHIRP channel, completed with what can be inferred."""
    fields = {k: v for k, v in chirp.channel_to_repeater_fields(ch).items() if k in _REPEATER_FIELDS}
    if not fields.get("output_mhz"):
        fields["output_mhz"] = ch.get("freq_mhz")
    name = (fields.get("name") or ch.get("name") or "").strip()
    if not name and fields.get("output_mhz"):
        name = f"{fields['output_mhz']:.4f}"
    fields["name"] = name
    # Channel names are often the repeater's callsign (IR2UFD): keep it as one.
    if not fields.get("callsign") and _CALLSIGN_RE.match(name.upper().replace(" ", "")):
        fields["callsign"] = name.upper().replace(" ", "")
    # Repeater lists often carry the locator in the comment.
    if not fields.get("locator") and fields.get("latitude") is None:
        text = " ".join(str(x) for x in (ch.get("comment"), fields.get("notes")) if x)
        found = _LOCATOR_IN_TEXT.search(text)
        if found:
            fields["locator"] = geo.normalize_locator(found.group(1))
    return fields


def _same_repeater(a_out: float | None, a_name: str, a_call: str | None,
                   b_out: float | None, b_name: str, b_call: str | None) -> bool:
    """Exact duplicate: same output (±0.5 kHz) and same name or same callsign."""
    if a_out is None or b_out is None or abs(a_out - b_out) > 0.0005:
        return False
    if a_name and a_name.lower() == (b_name or "").lower():
        return True
    return bool(a_call) and (a_call or "").upper() == (b_call or "").upper()


@bp.route("/import", methods=["GET", "POST"])
def import_chirp():
    s = h.session()
    if request.method == "GET":
        ref = reference_station(s)
        return render_template("repeaters/import.html", reference=ref)

    from shacklog.services import chirp

    upload = request.files.get("file")
    if upload is None or not upload.filename:
        flash("Choose a CHIRP CSV file first.", "error")
        return redirect(url_for(".import_chirp"))
    data = upload.read()
    try:
        channels = chirp.read_chirp_csv(decode_upload(data))
    except Exception as exc:  # malformed file: say so instead of a server error
        flash(f"Couldn't read {upload.filename} as a CHIRP CSV ({exc}). Export it again from "
              "CHIRP with File › Export.", "error")
        return redirect(url_for(".import_chirp"))
    _backup_before_import("before-chirp-import")

    country = request.form.get("country", "").strip() or None
    region = request.form.get("region", "").strip() or None
    source = request.form.get("source", "").strip() or f"CHIRP CSV {upload.filename}"
    include_simplex = bool(request.form.get("include_simplex"))

    existing = [(r.output_mhz, r.name, r.callsign) for r in h.live(Repeater).all()]
    created: list[Repeater] = []
    duplicates: list[str] = []
    simplex: list[str] = []
    errors: list[str] = []
    for ch in channels:
        label = f"#{ch.get('location', '?')} {ch.get('name') or ''}".strip()
        if not ch.get("freq_mhz"):
            errors.append(f"{label}: no frequency, skipped.")
            continue
        if is_simplex(ch) and not include_simplex:
            simplex.append(label)
            continue
        try:
            fields = repeater_fields_from_channel(chirp, ch)
        except Exception as exc:  # one odd row must not stop the whole import
            errors.append(f"{label}: {exc}")
            continue
        out, name, call = fields.get("output_mhz"), fields["name"], fields.get("callsign")
        if any(_same_repeater(out, name, call, *other) for other in existing):
            duplicates.append(f"{name} {out:.4f}")
            continue
        rep = Repeater(kind="FM", status="active")
        for key, value in fields.items():
            setattr(rep, key, value)
        rep.country = rep.country or country
        rep.region = rep.region or region
        rep.source = rep.source or source
        fill_position(rep)
        s.add(rep)
        created.append(rep)
        existing.append((out, name, call))

    summary = {"created": len(created), "duplicates": len(duplicates),
               "simplex_skipped": len(simplex), "errors": errors,
               "created_ids": [r.id for r in created]}
    s.add(ImportLog(kind="chirp-repeaters", filename=upload.filename, summary=json.dumps(summary)))
    s.commit()
    return render_template("repeaters/import_done.html", filename=upload.filename,
                           created=created, duplicates=duplicates, simplex=simplex,
                           errors=errors, include_simplex=include_simplex)


def _backup_before_import(reason: str) -> None:
    """MANDATES: back up before every import. Skipped only if the backup service is absent."""
    try:
        from shacklog.services import backup
    except ImportError:
        current_app.logger.warning("Backup service missing; importing without a backup.")
        return
    backup.create_backup(h.database(), current_app.config["PATHS"].backups, reason=reason)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

def _int_arg(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def _export_name(kind: str, result: flt.FilterResult | None = None) -> str:
    bits = [APP_NAME.lower(), kind]
    if result is not None and result.center_label and request.args.get("near"):
        bits.append(_slug(result.center_label))
        if request.args.get("radius"):
            bits.append(_slug(request.args["radius"]) + "km")
    bits.append(f"{utcnow():%Y%m%d}")
    return "-".join(b for b in bits if b) + ".csv"


def channel_for(chirp, rep: Repeater, location: int, name_length: int) -> dict:
    """``chirp.repeater_to_channel`` with a name length, tolerant of the older signature."""
    try:
        ch = chirp.repeater_to_channel(rep, location, name_length=name_length)
    except TypeError:
        ch = chirp.repeater_to_channel(rep, location)
    if ch.get("name"):
        ch["name"] = str(ch["name"])[:name_length]
    return ch


@bp.route("/export-chirp.csv")
def export_chirp():
    """CHIRP CSV of the currently filtered repeaters, ready to import into a radio."""
    from shacklog.services import chirp

    result = _with_reference(filtered_query())
    query = result.query.filter(Repeater.output_mhz.isnot(None))
    # Checkbox default on: absent means "on" unless the options form was submitted.
    fm_only = bool(request.args.get("fm_only")) or not request.args.get("opts")
    if fm_only:
        query = query.filter(Repeater.kind.in_(FM_KINDS))
    # Default: nearest first when a centre was asked for, else by frequency. "distance"
    # chosen explicitly without a centre uses the reference station (see _with_reference).
    order = request.args.get("order") or ("distance" if request.args.get("near") else "freq")
    if order == "distance" and result.distance is not None:
        query = query.order_by(result.distance.asc().nulls_last(), Repeater.output_mhz)
    elif order == "name":
        query = query.order_by(func.lower(Repeater.name), Repeater.output_mhz)
    else:
        query = query.order_by(Repeater.output_mhz, func.lower(Repeater.name))
    limit = _int_arg("max", 0, 0, 10000)
    if limit:
        query = query.limit(limit)
    start = _int_arg("start", 1, 0, 9999)
    name_length = _int_arg("name_len", 10, 3, 32)
    channels = [channel_for(chirp, rep, start + i, name_length)
                for i, rep in enumerate(query.all())]
    text = chirp.write_chirp_csv(channels)
    return Response(text, mimetype="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{_export_name("chirp", result)}"'})


#: Every user-facing repeater column, in a sensible order, for the plain CSV export.
CSV_COLUMNS = ["id", "name", "callsign", "ident", "kind", "output_mhz", "input_mhz",
               "ctcss_up", "ctcss_down", "dcs_code", "color_code", "talkgroups", "network",
               "echolink_node", "location_name", "locator", "latitude", "longitude",
               "altitude_m", "country", "region", "status", "source", "last_verified",
               "notes", "tags", "created_at", "updated_at"]


@bp.route("/export.csv")
def export_csv():
    """All fields of the currently filtered repeaters, for spreadsheets and backups."""
    result = filtered_query()
    reps = result.query.order_by(Repeater.output_mhz, Repeater.name).all()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS + ["offset_mhz"])
    for r in reps:
        row = []
        for c in CSV_COLUMNS:
            v = getattr(r, c)
            if isinstance(v, datetime):
                v = v.isoformat(sep=" ")
            elif isinstance(v, date):
                v = v.isoformat()
            row.append("" if v is None else v)
        row.append("" if r.offset_mhz is None else r.offset_mhz)
        writer.writerow(row)
    return Response(out.getvalue(), mimetype="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{_export_name("repeaters")}"'})
