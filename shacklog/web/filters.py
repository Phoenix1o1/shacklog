"""Declarative list filters: describe them once, get SQL + a filter bar + "active" chips.

Each list page declares a list of :class:`Filter`. :func:`apply_filters` reads
``request.args``, adds WHERE clauses to the query and reports which filters are active.
The macro ``filter_bar`` (templates/_filters.html) renders the form; the same query string
works for exports (ADIF/CSV), the map and saved filters, so every view of the data agrees.

Filter kinds
------------
``text``      words matched (LIKE) against one or several columns, all words must match
``call``      callsign: ``IK2*`` / ``?`` wildcards, otherwise "contains"
``choice``    equality with one value from a list (``choices``)
``ref``       equality with a referenced record id (``choices`` callable)
``date_from`` / ``date_to``   inclusive day bounds on a datetime/date column
``min`` / ``max``             numeric bounds
``tags``      every given tag must be present
``has``       "yes"/"no": column is (not) NULL/empty
``near``      ``near=<locator | lat,lon | station:<id>>`` + ``radius=<km>`` on a
              (lat, lon) column pair; also enables sorting by distance
``bbox``      ``bbox=minlat,minlon,maxlat,maxlon`` (used by "search this map area")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Callable

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Query

from shacklog.models import Station
from shacklog.services import geo


@dataclass
class Filter:
    name: str
    label: str
    kind: str
    columns: tuple = ()
    choices: Any = None  # list[(value, label)] or callable(session) -> list
    placeholder: str | None = None
    #: Shown under "More filters" (collapsed) unless active.
    advanced: bool = False
    #: Optional custom clause builder ``fn(value, session) -> SQL expression``.
    custom: Callable | None = None


@dataclass
class FilterResult:
    query: Query
    active: list[tuple[str, str, str]] = field(default_factory=list)  # (param, label, shown)
    #: SQL expression of the distance from the ``near`` centre, if one was given.
    distance: Any = None
    center: tuple[float, float] | None = None
    center_label: str | None = None
    errors: list[str] = field(default_factory=list)


def resolve_center(session, text: str) -> tuple[tuple[float, float] | None, str | None]:
    """Where "near" points to. Accepts, in order:

    ``station:<id>`` · a station name (case-insensitive) · a locator · coordinates.
    """
    text = (text or "").strip()
    station = None
    if text.startswith("station:"):
        station = session.get(Station, text.split(":", 1)[1])
    else:
        station = (session.query(Station).filter(Station.deleted_at.is_(None))
                   .filter(func.lower(Station.name) == text.lower()).first())
    if station is not None:
        return geo.position_from(station.locator, station.latitude, station.longitude), station.name
    pos = geo.parse_position(text)
    return pos, text if pos else None


def _like_words(columns, text: str):
    clauses = []
    for word in text.split():
        pattern = f"%{word}%"
        clauses.append(or_(*[col.ilike(pattern) for col in columns]))
    return and_(*clauses)


def _call_clause(column, text: str):
    text = text.strip().upper().replace(" ", "")
    if "*" in text or "?" in text:
        return column.like(text.replace("*", "%").replace("?", "_"))
    return column.like(f"%{text}%")


def _choice_label(choices, value) -> str:
    for key, label in choices or []:
        if str(key) == str(value):
            return label
    return str(value)


def apply_filters(query: Query, filters: list[Filter], args, session) -> FilterResult:
    result = FilterResult(query=query)
    for flt in filters:
        value = (args.get(flt.name) or "").strip()
        if not value:
            continue
        choices = flt.choices(session) if callable(flt.choices) else flt.choices
        shown = value
        clause = None
        try:
            if flt.custom is not None:
                clause = flt.custom(value, session)
            elif flt.kind == "text":
                clause = _like_words(flt.columns, value)
            elif flt.kind == "call":
                clause = _call_clause(flt.columns[0], value)
                shown = value.upper()
            elif flt.kind in ("choice", "ref"):
                clause = flt.columns[0] == value
                shown = _choice_label(choices, value)
            elif flt.kind == "date_from":
                day = datetime.fromisoformat(value[:10])
                clause = flt.columns[0] >= day
            elif flt.kind == "date_to":
                day = datetime.combine(datetime.fromisoformat(value[:10]).date(), time.min)
                clause = flt.columns[0] < day + timedelta(days=1)
            elif flt.kind == "min":
                clause = flt.columns[0] >= float(value.replace(",", "."))
            elif flt.kind == "max":
                clause = flt.columns[0] <= float(value.replace(",", "."))
            elif flt.kind == "tags":
                tag_clauses = [flt.columns[0].ilike(f"%{t.strip()}%")
                               for t in value.split(",") if t.strip()]
                clause = and_(*tag_clauses)
            elif flt.kind == "has":
                col = flt.columns[0]
                present = or_(col.is_(None), col == "")
                clause = ~present if value == "yes" else present
                shown = "yes" if value == "yes" else "no"
            elif flt.kind == "near":
                clause, shown = _near_clause(flt, value, args, session, result)
            elif flt.kind == "bbox":
                parts = [float(p) for p in value.split(",")]
                lat_col, lon_col = flt.columns
                clause = and_(lat_col.between(parts[0], parts[2]),
                              lon_col.between(parts[1], parts[3]))
                shown = "map area"
        except (ValueError, IndexError):
            result.errors.append(f"Couldn't understand {flt.label.lower()}: “{value}”")
            continue
        if clause is not None:
            result.query = result.query.filter(clause)
            result.active.append((flt.name, flt.label, shown))
    return result


def _near_clause(flt: Filter, value: str, args, session, result: FilterResult):
    """Distance filter. With no radius it only sets the centre (for sorting/columns)."""
    center, label = resolve_center(session, value)
    if center is None:
        raise ValueError(value)
    lat_col, lon_col = flt.columns
    dist = func.geo_dist_km(lat_col, lon_col, center[0], center[1])
    result.distance, result.center, result.center_label = dist, center, label
    radius_txt = (args.get("radius") or "").strip()
    if not radius_txt:
        return None, label
    radius = float(radius_txt.replace(",", "."))
    # Cheap bounding-box pre-filter (uses plain comparisons), then the exact distance.
    min_lat, min_lon, max_lat, max_lon = geo.bounding_box(center[0], center[1], radius)
    clause = and_(lat_col.between(min_lat, max_lat), lon_col.between(min_lon, max_lon),
                  dist <= radius)
    return clause, f"≤ {radius:g} km from {label}"


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------

@dataclass
class Sort:
    key: str
    label: str
    column: Any  # SQL expression; "distance" uses FilterResult.distance instead
    default_desc: bool = False


def apply_sort(result: FilterResult, sorts: list[Sort], args, default_key: str) -> tuple[Query, str, str]:
    """Order the query by ``?sort=<key>&dir=asc|desc``. Returns (query, key, direction)."""
    by_key = {s.key: s for s in sorts}
    key = args.get("sort") or default_key
    if key not in by_key or (key == "distance" and result.distance is None):
        key = default_key
    spec = by_key[key]
    direction = args.get("dir") or ("desc" if spec.default_desc else "asc")
    column = result.distance if key == "distance" else spec.column
    ordered = column.desc() if direction == "desc" else column.asc()
    if key == "distance":
        ordered = ordered.nulls_last()
    return result.query.order_by(ordered), key, direction


def resolve_filter_choices(filters: list[Filter], session) -> dict[str, Any]:
    """Evaluate callable choices once, for the filter bar template."""
    return {f.name: (f.choices(session) if callable(f.choices) else f.choices)
            for f in filters if f.choices is not None}


def station_center_choices(session) -> list[tuple[str, str]]:
    """Stations with a position, offered as suggestions in the "near" box (by name)."""
    stations = (session.query(Station).filter(Station.deleted_at.is_(None))
                .order_by(Station.kind, Station.name).all())
    return [(s.name, s.locator or "") for s in stations
            if geo.position_from(s.locator, s.latitude, s.longitude)]
