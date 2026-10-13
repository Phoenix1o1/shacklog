"""Shared helpers for views and templates: DB access, pagination, URLs, Jinja filters.

Views import from here rather than reaching into ``current_app.config`` directly, so the
plumbing lives in one place.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable

from flask import abort, current_app, g, request, url_for
from markupsafe import Markup, escape
from sqlalchemy.orm import Query, Session

from shacklog import db as dbmod
from shacklog.models import (
    Equipment,
    Repeater,
    Station,
    SyncMixin,
    choice_label,
    split_tags,
)
from shacklog.prefs import DEFAULTS
from shacklog.services import bands, geo
from shacklog.services.filestore import FileStore


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------

def database() -> dbmod.Database:
    return current_app.config["DB"]


def session() -> Session:
    """The ORM session of the current request (opened lazily, closed at teardown)."""
    if "db_session" not in g:
        g.db_session = database().session()
    return g.db_session


def close_session(_exc=None) -> None:
    s = g.pop("db_session", None)
    if s is not None:
        s.close()


def filestore() -> FileStore:
    return current_app.config["FILESTORE"]


def setting(key: str) -> Any:
    return dbmod.get_setting(session(), key, DEFAULTS.get(key))


def save_setting(key: str, value: Any) -> None:
    dbmod.set_setting(session(), key, value)


def get_or_404(model: type[SyncMixin], record_id: str, allow_deleted: bool = False) -> Any:
    obj = session().get(model, record_id)
    if obj is None or (obj.deleted_at is not None and not allow_deleted):
        abort(404)
    return obj


def live(model: type[SyncMixin]) -> Query:
    """Query of non-deleted rows of a model."""
    return session().query(model).filter(model.deleted_at.is_(None))


# ---------------------------------------------------------------------------
# Choices for <select> fields (callables taking a session)
# ---------------------------------------------------------------------------

def station_choices(s: Session) -> list[tuple[str, str]]:
    rows = (s.query(Station).filter(Station.deleted_at.is_(None))
            .order_by(Station.kind, Station.name).all())
    return [(r.id, r.name) for r in rows]


def equipment_choices(*kinds: str) -> Callable[[Session], list[tuple[str, str]]]:
    """Choices limited to some equipment kinds (all kinds if none given)."""

    def choices(s: Session) -> list[tuple[str, str]]:
        q = s.query(Equipment).filter(Equipment.deleted_at.is_(None))
        if kinds:
            q = q.filter(Equipment.kind.in_(kinds))
        rows = q.order_by(Equipment.name).all()
        return [(r.id, r.name + (" (retired)" if r.status == "retired" else "")) for r in rows]

    return choices


RADIO_KINDS = ("radio", "handheld", "receiver")
ANTENNA_KINDS = ("antenna",)


def repeater_choices(s: Session) -> list[tuple[str, str]]:
    rows = (s.query(Repeater).filter(Repeater.deleted_at.is_(None))
            .order_by(Repeater.country, Repeater.name).all())
    return [(r.id, r.display_name()) for r in rows]


def default_station(s: Session) -> Station | None:
    """Station preselected for new entries: the setting, else the first home station."""
    sid = dbmod.get_setting(s, "default_station_id")
    st = s.get(Station, sid) if sid else None
    if st is None or st.deleted_at is not None:
        st = (s.query(Station).filter(Station.deleted_at.is_(None))
              .order_by((Station.kind != "home"), Station.created_at).first())
    return st


# ---------------------------------------------------------------------------
# Pagination and URLs
# ---------------------------------------------------------------------------

@dataclass
class Page:
    items: list
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        return max(1, math.ceil(self.total / self.per_page))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def first_index(self) -> int:
        return 0 if self.total == 0 else (self.page - 1) * self.per_page + 1

    @property
    def last_index(self) -> int:
        return min(self.total, self.page * self.per_page)


def paginate(query: Query, per_page: int | None = None) -> Page:
    per_page = per_page or int(setting("page_size") or 50)
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    total = query.order_by(None).count()
    items = query.limit(per_page).offset((page - 1) * per_page).all()
    return Page(items, total, page, per_page)


def url_with(**changes) -> str:
    """Current URL with some query arguments changed (None removes one). Resets paging
    unless ``page`` itself is being changed."""
    args = request.args.to_dict(flat=True)
    if "page" not in changes:
        args.pop("page", None)
    for key, value in changes.items():
        if value is None or value == "":
            args.pop(key, None)
        else:
            args[key] = value
    return url_for(request.endpoint, **(request.view_args or {}), **args)


def query_string(exclude: tuple[str, ...] = ("page",)) -> str:
    """Current filters as a query string (for export links, map, saved filters)."""
    from urllib.parse import urlencode

    return urlencode([(k, v) for k, v in request.args.items(multi=True)
                      if k not in exclude and v != ""])


#: entity name → endpoint of its detail page (filled by each blueprint at import time).
ENTITY_VIEWS: dict[str, str] = {}
ENTITY_ICONS: dict[str, str] = {}


def register_entity_view(entity: str, endpoint: str, icon: str = "") -> None:
    ENTITY_VIEWS[entity] = endpoint
    ENTITY_ICONS[entity] = icon


def entity_url(entity: str, entity_id: str) -> str | None:
    endpoint = ENTITY_VIEWS.get(entity)
    if endpoint is None:
        return None
    try:
        return url_for(endpoint, record_id=entity_id)
    except Exception:  # blueprint not loaded
        return None


# ---------------------------------------------------------------------------
# Template filters
# ---------------------------------------------------------------------------

def f_dt(value: datetime | None, with_seconds: bool = False) -> str:
    if not value:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M")


def f_day(value: date | datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else ""


def f_time(value: datetime | None) -> str:
    return value.strftime("%H:%M") if value else ""


def f_freq(value: float | None, digits: int = 4) -> str:
    return bands.format_freq(value, digits)


def f_km(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.1f} km" if value < 10 else f"{value:,.0f} km".replace(",", " ")


def f_bearing(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.0f}° {geo.compass_point(value)}"


def f_filesize(size: int | None) -> str:
    if not size:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def f_highlight(value: str | None) -> Markup:
    """Search snippet: escape, then turn the [[…]] match markers into <mark>."""
    safe = str(escape(value or ""))
    return Markup(safe.replace("[[", "<mark>").replace("]]", "</mark>"))


_URL_RE = re.compile(r"(https?://[^\s<]+)")


def f_textblock(value: str | None) -> Markup:
    """Multi-line text: escaped, paragraphs and line breaks kept, URLs clickable."""
    if not value:
        return Markup("")
    paragraphs = re.split(r"\n\s*\n", value.strip())
    html = []
    for para in paragraphs:
        safe = str(escape(para))
        safe = _URL_RE.sub(r'<a href="\1" rel="noopener" target="_blank">\1</a>', safe)
        html.append("<p>" + safe.replace("\n", "<br>") + "</p>")
    return Markup("\n".join(html))


def f_label(value: str | None, choices) -> str:
    return choice_label(choices, value)


def f_safe_url(value) -> str:
    """Only http(s) links leave the app: a synced or imported "javascript:" URL becomes "#"."""
    text = str(value or "").strip()
    return text if text.lower().startswith(("http://", "https://")) else "#"


def home_position() -> tuple[float, float] | None:
    """Where "I" am for beam headings: the default station, else the reference station."""
    for key in ("default_station_id", "reference_station_id"):
        station_id = setting(key)
        station = session().get(Station, station_id) if station_id else None
        if station is not None and station.deleted_at is None:
            pos = geo.position_from(station.locator, station.latitude, station.longitude)
            if pos:
                return pos
    return None


def country_of(call: str | None) -> dict | None:
    """Country of a callsign with the beam heading and distance to it from my station.

    ``{"entity": cty.Entity, "bearing": 123.4 | None, "km": 1234.5 | None}``, or None
    when there is no country list or the callsign doesn't match it.
    """
    from shacklog.services import cty

    entity = cty.lookup(call)
    if entity is None:
        return None
    result = {"entity": entity, "bearing": None, "km": None}
    home = home_position()
    if home is not None:
        result["bearing"] = geo.bearing_deg(*home, entity.latitude, entity.longitude)
        result["km"] = geo.haversine_km(*home, entity.latitude, entity.longitude)
    return result


def register_template_helpers(app) -> None:
    app.jinja_env.filters.update(
        dt=f_dt, day=f_day, hm=f_time, freq=f_freq, km=f_km, bearing=f_bearing,
        filesize=f_filesize, highlight=f_highlight, textblock=f_textblock,
        label=f_label, tags=split_tags, safe_url=f_safe_url,
    )
    from shacklog.web.forms import section_is_open

    app.jinja_env.globals.update(
        url_with=url_with, query_string=query_string, entity_url=entity_url,
        entity_icons=ENTITY_ICONS, display_mode=bands.display_mode,
        section_is_open=section_is_open,
    )
