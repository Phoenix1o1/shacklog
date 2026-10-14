"""Map of QSOs, listening entries, repeaters and stations (Leaflet + OpenStreetMap tiles).

Tiles are served by ShackLog itself (``/map/tiles/z/x/y``, services/tiles.py): from
offline MBTiles files, the tile cache, or the tile server, so the map works offline for
the places already viewed, and fully with an MBTiles file.

The page itself is a shell: a JSON config block, the vendored Leaflet and ``js/map.js``.
Points come from ``/map/data.json?layer=<qso|swl|repeater|station>&<filters>``, which runs
the **same** ``filtered_query()`` as that section's list page, so "Show on map" from a
filtered list shows exactly those records, and "List what's in this area" goes back to the
list with a ``bbox`` filter added.

Section modules are imported lazily (``importlib``) because they are written separately
and a missing one must only disable its layer, not the map.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlencode

from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request, url_for
from markupsafe import escape
from sqlalchemy import and_, or_

from shacklog.models import STATION_KINDS, Qso, Repeater, Station, SwlEntry, choice_label
from shacklog.services import bands, geo, tiles
from shacklog.web import helpers as h

bp = Blueprint("map", __name__, url_prefix="/map")

#: Most points sent per layer. Leaflet's canvas renderer copes with more, but a phone on
#: a slow link shouldn't download a whole 20-year log to draw a map.
MAX_POINTS = 3000


#: Query arguments that belong to the map, not to a section's filters.
MAP_ARGS = {"layer", "page", "sort", "dir"}


@dataclass
class Layer:
    key: str
    label: str
    module: str  # shacklog.web.views.<module>
    index_endpoint: str
    lines: bool  # entries carry "my position" too: lines can be drawn


LAYERS = [
    Layer("qso", "QSOs", "qsos", "qsos.index", True),
    Layer("swl", "Heard", "swl", "swl.index", True),
    Layer("repeater", "Repeaters", "repeaters", "repeaters.index", False),
    Layer("station", "My stations", "stations", "stations.index", False),
]
LAYER_BY_KEY = {layer.key: layer for layer in LAYERS}


def section_module(layer: Layer):
    """The section's view module, or None if it isn't part of this build."""
    try:
        return importlib.import_module(f"shacklog.web.views.{layer.module}")
    except ModuleNotFoundError as exc:
        if exc.name == f"shacklog.web.views.{layer.module}":
            return None
        raise


def _has_bbox(module) -> bool:
    return any(getattr(f, "name", None) == "bbox" for f in getattr(module, "FILTERS", []) or [])


def filter_args() -> list[tuple[str, str]]:
    """The section filter parameters of the current request (map arguments removed)."""
    return [(k, v) for k, v in request.args.items(multi=True) if k not in MAP_ARGS and v != ""]


# ---------------------------------------------------------------------------
# Points per layer
# ---------------------------------------------------------------------------

def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 5)


def _filtered(layer: Layer, fallback):
    """``(query, FilterResult or None)`` for a layer: the section's filters when it has them."""
    module = section_module(layer)
    if module is not None and hasattr(module, "filtered_query"):
        result = module.filtered_query()
        return result.query, result
    return fallback(), None


def _qso_points() -> tuple[list[dict], dict]:
    query, result = _filtered(LAYER_BY_KEY["qso"], lambda: h.live(Qso))
    located = query.filter(Qso.latitude.isnot(None), Qso.longitude.isnot(None))
    rows = (located.with_entities(Qso.id, Qso.call, Qso.time_on, Qso.band, Qso.mode, Qso.submode,
                                  Qso.freq_mhz, Qso.latitude, Qso.longitude, Qso.my_latitude,
                                  Qso.my_longitude, Qso.distance_km)
            .order_by(Qso.time_on.desc()).limit(MAX_POINTS + 1).all())
    points = []
    for r in rows[:MAX_POINTS]:
        sub = " · ".join(filter(None, [h.f_dt(r.time_on) + " UTC", r.band,
                                       bands.display_mode(r.mode, r.submode),
                                       h.f_km(r.distance_km)]))
        points.append({
            "id": r.id, "lat": _round(r.latitude), "lon": _round(r.longitude),
            "title": r.call, "sub": sub, "url": url_for("qsos.view", record_id=r.id),
            "my": ([_round(r.my_latitude), _round(r.my_longitude)]
                   if r.my_latitude is not None and r.my_longitude is not None else None),
        })
    return points, _counts(query, located, len(rows), result)


def _swl_points() -> tuple[list[dict], dict]:
    query, result = _filtered(LAYER_BY_KEY["swl"], lambda: h.live(SwlEntry))
    located = query.filter(SwlEntry.latitude.isnot(None), SwlEntry.longitude.isnot(None))
    rows = (located.with_entities(SwlEntry.id, SwlEntry.heard, SwlEntry.category,
                                  SwlEntry.time_utc, SwlEntry.freq_mhz, SwlEntry.mode,
                                  SwlEntry.latitude, SwlEntry.longitude, SwlEntry.my_latitude,
                                  SwlEntry.my_longitude, SwlEntry.distance_km)
            .order_by(SwlEntry.time_utc.desc()).limit(MAX_POINTS + 1).all())
    points = []
    for r in rows[:MAX_POINTS]:
        sub = " · ".join(filter(None, [h.f_dt(r.time_utc) + " UTC",
                                       (h.f_freq(r.freq_mhz) + " MHz") if r.freq_mhz else None,
                                       r.mode, h.f_km(r.distance_km)]))
        points.append({
            "id": r.id, "lat": _round(r.latitude), "lon": _round(r.longitude),
            "title": r.heard or r.category, "sub": sub,
            "url": url_for("swl.view", record_id=r.id),
            "my": ([_round(r.my_latitude), _round(r.my_longitude)]
                   if r.my_latitude is not None and r.my_longitude is not None else None),
        })
    return points, _counts(query, located, len(rows), result)


def _repeater_points() -> tuple[list[dict], dict]:
    query, result = _filtered(LAYER_BY_KEY["repeater"], lambda: h.live(Repeater))
    # A repeater may only have a locator; its square's centre is better than nothing.
    located = query.filter(or_(and_(Repeater.latitude.isnot(None), Repeater.longitude.isnot(None)),
                               and_(Repeater.locator.isnot(None), Repeater.locator != "")))
    rows = located.order_by(Repeater.name).limit(MAX_POINTS + 1).all()
    points = []
    for r in rows[:MAX_POINTS]:
        pos = geo.position_from(r.locator, r.latitude, r.longitude)
        if pos is None:
            continue
        bits = [r.callsign, r.ident, r.kind]
        if r.output_mhz:
            bits.append(f"{h.f_freq(r.output_mhz)} MHz")
        if r.offset_mhz:
            bits.append(f"{r.offset_mhz:+g}")
        if r.ctcss_up:
            bits.append(f"{r.ctcss_up:g} Hz")
        points.append({
            "id": r.id, "lat": _round(pos[0]), "lon": _round(pos[1]), "title": r.name,
            "sub": " · ".join(b for b in bits if b), "url": h.entity_url("repeater", r.id),
            "my": None, "status": r.status,
        })
    return points, _counts(query, located, len(rows), result)


def _station_points() -> tuple[list[dict], dict]:
    query, result = _filtered(LAYER_BY_KEY["station"], lambda: h.live(Station))
    rows = query.order_by(Station.name).limit(MAX_POINTS + 1).all()
    points = []
    for st in rows[:MAX_POINTS]:
        pos = geo.position_from(st.locator, st.latitude, st.longitude)
        if pos is None:
            continue
        sub = " · ".join(filter(None, [choice_label(STATION_KINDS, st.kind), st.callsign,
                                       st.locator]))
        points.append({
            "id": st.id, "lat": _round(pos[0]), "lon": _round(pos[1]), "title": st.name,
            "sub": sub, "url": h.entity_url("station", st.id), "my": None, "kind": st.kind,
        })
    total = min(len(rows), MAX_POINTS)
    return points, {"total": total, "shown": len(points), "capped": len(rows) > MAX_POINTS,
                    "without_position": total - len(points),
                    "active": [list(a) for a in (result.active if result else [])],
                    "errors": list(result.errors) if result else []}


def _counts(query, located, fetched: int, result) -> dict:
    total = located.order_by(None).count() if fetched > MAX_POINTS else fetched
    everything = query.order_by(None).count()
    return {
        "total": total, "shown": min(fetched, MAX_POINTS), "capped": fetched > MAX_POINTS,
        "without_position": max(0, everything - total),
        "active": [list(a) for a in (result.active if result else [])],
        "errors": list(result.errors) if result else [],
    }


POINT_BUILDERS: dict[str, Callable[[], tuple[list[dict], dict]]] = {
    "qso": _qso_points,
    "swl": _swl_points,
    "repeater": _repeater_points,
    "station": _station_points,
}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.route("/data.json")
def data():
    key = request.args.get("layer", "")
    builder = POINT_BUILDERS.get(key)
    if builder is None:
        abort(400, description="Unknown map layer.")
    points, info = builder()
    return jsonify(layer=key, points=points, max=MAX_POINTS, **info)


def _focus(result) -> dict | None:
    """Centre and radius of a "near" filter, so the map can centre and draw the circle."""
    if result is None or result.center is None:
        return None
    try:
        radius = float((request.args.get("radius") or "0").replace(",", "."))
    except ValueError:
        radius = 0.0
    return {"lat": result.center[0], "lon": result.center[1], "radius_km": radius,
            "label": result.center_label}


@bp.route("", strict_slashes=False)
def index():
    s = h.session()
    requested = LAYER_BY_KEY.get(request.args.get("layer", ""))
    args = filter_args() if requested else []
    result = None
    if requested is not None:
        module = section_module(requested)
        if module is not None and hasattr(module, "filtered_query"):
            result = module.filtered_query()  # for the chips and the "near" circle
    layers = []
    for layer in LAYERS:
        module = section_module(layer)
        is_requested = requested is not None and layer.key == requested.key
        qs = urlencode(args) if is_requested else ""
        list_url = None
        if module is not None and _has_bbox(module):
            try:
                list_url = url_for(layer.index_endpoint)
            except Exception:  # blueprint not registered in this app
                list_url = None
        layers.append({
            "key": layer.key, "label": layer.label,
            "on": is_requested or requested is None,
            "filters": qs, "lines": layer.lines, "listUrl": list_url,
        })
    station = h.default_station(s)
    home = None
    if station is not None:
        pos = geo.position_from(station.locator, station.latitude, station.longitude)
        if pos:
            home = {"lat": pos[0], "lon": pos[1], "name": station.name}
    config = {
        "dataUrl": url_for(".data"),
        "tiles": {"url": url_for(".tile", z=0, x=0, y=0).replace("/0/0/0", "/{z}/{x}/{y}"),
                  "attribution": _attribution(), "maxZoom": tiles.MAX_ZOOM},
        "layers": layers,
        "home": home,
        "focus": _focus(result),
        "max": MAX_POINTS,
    }
    back_url = None
    if requested is not None:
        try:
            back_url = url_for(requested.index_endpoint) + (f"?{urlencode(args)}" if args else "")
        except Exception:  # that section isn't part of this build
            back_url = None
    return render_template("map/index.html", config=config, requested=requested,
                           active=result.active if result else [],
                           errors=result.errors if result else [], back_url=back_url,
                           list_layers=[layer for layer in layers if layer["listUrl"]])


# ---------------------------------------------------------------------------
# Tiles
# ---------------------------------------------------------------------------

def tile_source() -> tiles.TileSource:
    """The tile source as configured in Settings › Import, export & backups › Map."""
    return tiles.TileSource(
        current_app.config["PATHS"].data_dir, url=h.setting("map_tile_url"),
        online=bool(h.setting("map_tiles_online")),
        cache_limit_mb=int(h.setting("map_tile_cache_mb") or 0))


def _attribution() -> str:
    """OpenStreetMap's credit, plus the credits written in the offline files (as text:
    a file's metadata is not trusted as HTML)."""
    parts = [tiles.OSM_ATTRIBUTION]
    for path in tiles.mbtiles_files(current_app.config["PATHS"].data_dir):
        text = tiles.mbtiles_info(path).get("attribution")
        if text:
            parts.append(str(escape(text)))
    return " · ".join(dict.fromkeys(parts))


@bp.route("/tiles/<int:z>/<int:x>/<int:y>")
def tile(z: int, x: int, y: int):
    found = tile_source().get(z, x, y)
    if found is None:
        abort(404)
    response = Response(found.data, mimetype=found.content_type)
    # The browser may keep it a day; ShackLog's own cache keeps it much longer.
    response.headers["Cache-Control"] = "private, max-age=86400"
    response.headers["X-Tile-Source"] = found.source
    return response
