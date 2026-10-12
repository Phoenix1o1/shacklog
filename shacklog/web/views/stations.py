"""Stations: the places I operate from (home, a fixed second QTH, portable, mobile).

A station holds a position (locator and/or coordinates), my callsign there and the
default radio, antenna and power for new log entries. Its page answers "what did I do
from here?" (QSOs, listening entries) and "what can I reach from here?" (nearest
repeaters, with a shortcut to a CHIRP export of them), and lets Andrea choose the default
station for new entries and the reference point for distances.

Also home of two small helpers other reference sections reuse:
:func:`fill_position` (complete locator ↔ coordinates) and :func:`reference_station`.
"""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from sqlalchemy import func, null

from shacklog.models import STATION_KINDS, Qso, Repeater, Station, SwlEntry
from shacklog.services import geo
from shacklog.services.links import links_for
from shacklog.web import filters as flt
from shacklog.web import forms
from shacklog.web import helpers as h
from shacklog.web.forms import Field, Section

bp = Blueprint("stations", __name__, url_prefix="/stations")
h.register_entity_view("station", "stations.view")

FORM = [
    Section("Station", [
        Field("name", "Name", required=True, width="half", placeholder="Milano, Grenoble, Car…"),
        Field("kind", "Kind", type="select", choices=STATION_KINDS, blank=False, width="half"),
        Field("callsign", "My callsign here", type="upper", width="half", placeholder="IU2…, F/IU2…"),
        Field("operator", "Operator", type="upper", width="half",
              help="Only if someone else usually operates from here."),
    ]),
    Section("Location", [
        Field("location_name", "Place", width="half", placeholder="Town, building, summit…"),
        Field("locator", "Locator", type="locator", width="quarter", placeholder="JN45ol"),
        Field("altitude_m", "Altitude (m)", type="float", width="quarter"),
        Field("latitude", "Latitude", type="lat", width="quarter"),
        Field("longitude", "Longitude", type="lon", width="quarter"),
        Field("country", "Country", width="quarter"),
        Field("region", "Region", width="quarter"),
    ], note="Give a locator or coordinates; the other is computed when you save."),
    Section("Default setup for new entries", [
        Field("radio_id", "Radio", type="select", choices=h.equipment_choices(*h.RADIO_KINDS),
              width="third"),
        Field("antenna_id", "Antenna", type="select",
              choices=h.equipment_choices(*h.ANTENNA_KINDS), width="third"),
        Field("power_w", "Power (W)", type="power", width="third"),
    ]),
    Section("Notes", [
        Field("notes", "Notes", type="textarea", width="full"),
    ]),
]

FILTERS = [
    flt.Filter("q", "Text", "text", (Station.name, Station.callsign, Station.location_name,
                                     Station.locator, Station.country, Station.region,
                                     Station.notes), placeholder="Name, place, locator…"),
    flt.Filter("kind", "Kind", "choice", (Station.kind,), choices=STATION_KINDS),
]

SORTS = [
    flt.Sort("name", "Name", func.lower(Station.name)),
    flt.Sort("kind", "Kind", Station.kind),
    flt.Sort("distance", "Distance", None),
]


# ---------------------------------------------------------------------------
# Shared helpers (also used by repeaters and contacts)
# ---------------------------------------------------------------------------

def check_coordinates(values: dict, errors: dict) -> None:
    """A position needs both halves: flag a latitude without longitude and vice versa."""
    lat, lon = values.get("latitude"), values.get("longitude")
    if (lat is None) != (lon is None) and "latitude" not in errors and "longitude" not in errors:
        missing = "longitude" if lon is None else "latitude"
        errors[missing] = "Give both latitude and longitude, or neither."


def fill_position(obj, before: tuple | None = None) -> None:
    """Complete ``locator`` from coordinates or coordinates from ``locator``.

    ``before`` is ``(locator, latitude, longitude)`` as stored before an edit. When only
    one side was edited, the other side is recomputed instead of being left stale (moving
    a repeater by typing a new locator must also move its coordinates).
    """
    loc = obj.locator
    has_coords = obj.latitude is not None and obj.longitude is not None
    if before is not None:
        old_loc, old_lat, old_lon = before
        loc_changed = (loc or None) != (old_loc or None)
        coords_changed = (obj.latitude, obj.longitude) != (old_lat, old_lon)
        if loc_changed and not coords_changed and loc:
            has_coords = False  # coordinates belong to the old locator
        elif coords_changed and not loc_changed and has_coords:
            loc = None  # locator belongs to the old coordinates
    if has_coords and not loc:
        obj.locator = geo.latlon_to_locator(obj.latitude, obj.longitude, 6)
    elif loc and not has_coords:
        obj.locator = loc
        obj.latitude, obj.longitude = geo.locator_to_latlon(loc)


def station_position(station: Station | None) -> tuple[float, float] | None:
    if station is None:
        return None
    return geo.position_from(station.locator, station.latitude, station.longitude)


def reference_station(s) -> Station | None:
    """The station distances are measured from when no other centre is given."""
    sid = h.setting("reference_station_id")
    st = s.get(Station, sid) if sid else None
    if st is None or st.deleted_at is not None:
        st = h.default_station(s)
    return st


def filtered_query():
    """Stations matching the current request's filters."""
    return flt.apply_filters(h.live(Station), FILTERS, request.args, h.session())


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    s = h.session()
    result = filtered_query()
    ref = reference_station(s)
    ref_pos = station_position(ref)
    if ref_pos is not None:
        result.distance = func.geo_dist_km(Station.latitude, Station.longitude, *ref_pos)
        result.center, result.center_label = ref_pos, ref.name
    query, sort_key, sort_dir = flt.apply_sort(result, SORTS, request.args, "kind")
    if sort_key == "kind":
        query = query.order_by(func.lower(Station.name))
    dist = result.distance.label("dist") if result.distance is not None else null().label("dist")
    page = h.paginate(query.add_columns(dist))
    return render_template(
        "stations/index.html", page=page, filters=FILTERS,
        fchoices=flt.resolve_filter_choices(FILTERS, s), fresult=result,
        sort_key=sort_key, sort_dir=sort_dir, reference=ref,
        default_id=h.setting("default_station_id"),
    )


# ---------------------------------------------------------------------------
# Create / edit / delete
# ---------------------------------------------------------------------------

def _render_form(station: Station | None, values, errors, title: str):
    return render_template(
        "stations/form.html", station=station, sections=FORM, values=values, errors=errors,
        choices=forms.resolve_choices(FORM, h.session()), title=title,
    )


def _parse():
    values, errors, raw = forms.parse(FORM, request.form)
    check_coordinates(values, errors)
    return values, errors, raw


@bp.route("/new", methods=["GET", "POST"])
def new():
    s = h.session()
    if request.method == "POST":
        values, errors, raw = _parse()
        if errors:
            return _render_form(None, raw, errors, "New station"), 422
        station = Station()
        forms.apply(station, values)
        fill_position(station)
        s.add(station)
        s.commit()
        flash(f"Added station {station.name}.", "ok")
        return redirect(url_for(".view", record_id=station.id))
    template = Station(kind=request.args.get("kind") or "portable")
    home = h.default_station(s)
    if home is not None:  # same callsign as at home is the usual case
        template.callsign = home.callsign
    return _render_form(None, forms.values_from_obj(FORM, template), {}, "New station")


@bp.route("/<record_id>/edit", methods=["GET", "POST"])
def edit(record_id: str):
    s = h.session()
    station = h.get_or_404(Station, record_id)
    if request.method == "POST":
        values, errors, raw = _parse()
        if errors:
            return _render_form(station, raw, errors, f"Edit {station.name}"), 422
        before = (station.locator, station.latitude, station.longitude)
        forms.apply(station, values)
        fill_position(station, before)
        s.commit()
        flash("Saved.", "ok")
        return redirect(url_for(".view", record_id=station.id))
    return _render_form(station, forms.values_from_obj(FORM, station), {},
                        f"Edit {station.name}")


@bp.route("/<record_id>/delete", methods=["POST"])
def delete(record_id: str):
    station = h.get_or_404(Station, record_id)
    station.soft_delete()
    # Settings must not keep pointing at a station in the trash.
    for key in ("default_station_id", "reference_station_id"):
        if h.setting(key) == station.id:
            h.save_setting(key, None)
    h.session().commit()
    flash(f"{station.name} moved to the trash. Log entries keep their recorded position.", "ok")
    return redirect(url_for(".index"))


@bp.route("/<record_id>/make-default", methods=["POST"])
def make_default(record_id: str):
    station = h.get_or_404(Station, record_id)
    h.save_setting("default_station_id", station.id)
    h.session().commit()
    flash(f"New entries now start at {station.name}.", "ok")
    return redirect(url_for(".view", record_id=station.id))


@bp.route("/<record_id>/make-reference", methods=["POST"])
def make_reference(record_id: str):
    station = h.get_or_404(Station, record_id)
    h.save_setting("reference_station_id", station.id)
    h.session().commit()
    flash(f"Distances are now measured from {station.name} unless a list says otherwise.", "ok")
    return redirect(url_for(".view", record_id=station.id))


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

@bp.route("/<record_id>")
def view(record_id: str):
    s = h.session()
    station = h.get_or_404(Station, record_id)
    pos = station_position(station)
    qsos = h.live(Qso).filter(Qso.station_id == station.id)
    swl = h.live(SwlEntry).filter(SwlEntry.station_id == station.id)
    nearest = []
    if pos is not None:
        dist = func.geo_dist_km(Repeater.latitude, Repeater.longitude, pos[0], pos[1])
        rows = (h.live(Repeater)
                .filter(Repeater.latitude.isnot(None), Repeater.longitude.isnot(None))
                .add_columns(dist.label("km")).order_by(dist).limit(10).all())
        nearest = [(rep, km, geo.bearing_deg(pos[0], pos[1], rep.latitude, rep.longitude))
                   for rep, km in rows]
    return render_template(
        "stations/view.html", station=station, pos=pos,
        qso_count=qsos.count(), swl_count=swl.count(),
        latest_qsos=qsos.order_by(Qso.time_on.desc()).limit(5).all(),
        nearest=nearest, linked=links_for(s, "station", station.id),
        is_default=h.setting("default_station_id") == station.id,
        is_reference=h.setting("reference_station_id") == station.id,
    )
