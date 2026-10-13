"""Statistics: what the log adds up to. Plain SQL aggregates drawn as CSS bars (no charts).

Filters: ``year`` (calendar year, UTC) and ``station`` (my station). Every section uses the
same filtered base query, so the numbers on one page always agree with each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from flask import Blueprint, render_template, request, url_for
from sqlalchemy import func

from shacklog.models import (
    SWL_CATEGORIES,
    Equipment,
    Qso,
    Repeater,
    Station,
    SwlEntry,
    choice_label,
    utcnow,
)
from shacklog.services import bands
from shacklog.web import helpers as h

bp = Blueprint("stats", __name__, url_prefix="/stats")

TOP_CALLS = 20
TOP_REPEATERS = 15
TOP_COUNTRIES = 25
MONTHS = 24


@dataclass
class Bar:
    label: str
    value: int
    pct: float  # width of the bar, % of the largest value in its list
    url: str | None = None


def bars(pairs: list[tuple[str, int]], url_for_label=None) -> list[Bar]:
    top = max((v for _, v in pairs), default=0) or 1
    return [Bar(str(label), int(value), round(100.0 * value / top, 1),
                url_for_label(label) if url_for_label else None) for label, value in pairs]


def _band_order(name: str | None) -> int:
    try:
        return bands.BAND_NAMES.index(name)
    except ValueError:
        return len(bands.BAND_NAMES)


def _year_filter() -> int | None:
    try:
        year = int(request.args.get("year", ""))
    except ValueError:
        return None
    return year if 1900 <= year <= 2200 else None


def _years(s) -> list[int]:
    """Years with any QSO or listening entry, newest first (the year selector)."""
    years = set()
    for column, model in ((Qso.time_on, Qso), (SwlEntry.time_utc, SwlEntry)):
        rows = (s.query(func.strftime("%Y", column)).filter(model.deleted_at.is_(None))
                .distinct().all())
        years.update(int(y) for (y,) in rows if y)
    years.add(utcnow().year)
    return sorted(years, reverse=True)


def _months_back(now: datetime, count: int) -> list[str]:
    """``["2024-11", …, "2026-10"]``: the last ``count`` months, oldest first."""
    year, month = now.year, now.month
    out = []
    for _ in range(count):
        out.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(out))


@bp.route("/")
def index():
    s = h.session()
    year = _year_filter()
    station_id = request.args.get("station") or None
    station = s.get(Station, station_id) if station_id else None

    q = s.query(Qso).filter(Qso.deleted_at.is_(None))
    w = s.query(SwlEntry).filter(SwlEntry.deleted_at.is_(None))
    if year:
        start, end = datetime(year, 1, 1), datetime(year + 1, 1, 1)
        q = q.filter(Qso.time_on >= start, Qso.time_on < end)
        w = w.filter(SwlEntry.time_utc >= start, SwlEntry.time_utc < end)
    if station_id:
        q = q.filter(Qso.station_id == station_id)
        w = w.filter(SwlEntry.station_id == station_id)
    qsub = q.subquery()
    wsub = w.subquery()

    def grouped(column, sub=qsub, limit=None, order_by_count=True):
        query = s.query(column, func.count()).select_from(sub).group_by(column)
        query = query.order_by(func.count().desc(), column) if order_by_count else query.order_by(column)
        if limit:
            query = query.limit(limit)
        return query.all()

    total_qsos = s.query(func.count()).select_from(qsub).scalar() or 0
    total_swl = s.query(func.count()).select_from(wsub).scalar() or 0
    unique_calls = s.query(func.count(func.distinct(qsub.c.call))).scalar() or 0

    # QSOs by band, in band order (2m before 70cm), unknown last.
    by_band = sorted(grouped(qsub.c.band), key=lambda r: _band_order(r[0]))
    by_band = [(b or "unknown", n) for b, n in by_band]

    # Mode as shown everywhere else: the submode when there is one (DMR, not DIGITALVOICE).
    mode_col = func.coalesce(func.nullif(qsub.c.submode, ""), qsub.c.mode, "unknown")
    by_mode = grouped(mode_col)

    year_col = func.strftime("%Y", qsub.c.time_on)
    by_year = [(y, n) for y, n in grouped(year_col, order_by_count=False) if y]

    # Last 24 months, zero-filled, ending with the current month (or December of the
    # selected year).
    now = utcnow()
    end_month = datetime(year, 12, 1) if year and year < now.year else now
    months = _months_back(end_month, MONTHS)
    month_col = func.strftime("%Y-%m", Qso.time_on)
    mq = (s.query(month_col, func.count()).filter(Qso.deleted_at.is_(None),
                                                  month_col >= months[0], month_col <= months[-1]))
    if station_id:
        mq = mq.filter(Qso.station_id == station_id)
    month_counts = dict(mq.group_by(month_col).all())
    by_month = [(m, month_counts.get(m, 0)) for m in months]

    top_calls = grouped(qsub.c.call, limit=TOP_CALLS)

    # Countries (filled in from the callsign when the country list is installed).
    country_col = func.nullif(func.trim(qsub.c.country), "")
    countries = s.query(func.count(func.distinct(country_col))).scalar() or 0
    by_country = [(c, n) for c, n in grouped(country_col, limit=TOP_COUNTRIES) if c]

    # ODX: the longest QSO per band. A window function picks one row per band.
    rank = func.row_number().over(partition_by=qsub.c.band,
                                  order_by=(qsub.c.distance_km.desc(), qsub.c.time_on)).label("rk")
    ranked = (s.query(qsub.c.id, qsub.c.band, qsub.c.call, qsub.c.distance_km, qsub.c.time_on,
                      qsub.c.locator, rank)
              .filter(qsub.c.distance_km.isnot(None)).subquery())
    odx = sorted(s.query(ranked).filter(ranked.c.rk == 1).all(), key=lambda r: _band_order(r.band))

    rep_rows = (s.query(Repeater, func.count()).select_from(qsub)
                .join(Repeater, Repeater.id == qsub.c.repeater_id)
                .group_by(Repeater.id).order_by(func.count().desc(), Repeater.name)
                .limit(TOP_REPEATERS).all())

    def by_ref(column, model):
        rows = grouped(column)
        names = {}
        ids = [r for r, _ in rows if r]
        if ids:
            names = {o.id: o.name for o in s.query(model).filter(model.id.in_(ids))}
        return [((names.get(r) or "unknown") if r else "not set", n) for r, n in rows]

    per_station = by_ref(qsub.c.station_id, Station)
    per_radio = by_ref(qsub.c.radio_id, Equipment)
    per_antenna = by_ref(qsub.c.antenna_id, Equipment)

    swl_by_category = [(choice_label(SWL_CATEGORIES, c) or "unknown", n)
                       for c, n in grouped(wsub.c.category, sub=wsub)]
    swl_unique = s.query(func.count(func.distinct(func.upper(wsub.c.heard)))).scalar() or 0

    return render_template(
        "stats/index.html",
        year=year, station=station, years=_years(s), stations=h.station_choices(s),
        total_qsos=total_qsos, total_swl=total_swl, unique_calls=unique_calls,
        countries=countries,
        by_country=bars(by_country, lambda c: url_for("qsos.index", country=c)),
        swl_unique=swl_unique,
        by_band=bars(by_band), by_mode=bars(by_mode), by_year=bars(by_year),
        by_month=bars(by_month), top_calls=top_calls, odx=odx,
        repeaters=[(r, n) for r, n in rep_rows],
        per_station=bars(per_station), per_radio=bars(per_radio), per_antenna=bars(per_antenna),
        swl_by_category=bars(swl_by_category), months=MONTHS,
    )
