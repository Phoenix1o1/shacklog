"""Contacts and callsign pages: everything known about another operator in one place.

Two things live here:

* **Contact cards** — my own notes about a callsign (name, QTH, e-mail, notes). One live
  card per callsign.
* **Callsign pages** (``/contacts/call/<call>``) — the card plus every QSO, every
  listening entry where that call was heard, repeaters with that callsign, and summary
  statistics. Every callsign in the log gets a page, card or not.

The index lists the cards and, below, every callsign in the log aggregated from the QSOs
(count, first/last contact, latest name/QTH, bands), so "who have I worked most?" and
"who was that F1… in Grenoble?" take one click.
"""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from markupsafe import Markup, escape
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from shacklog.models import Contact, Qso, Repeater, SwlEntry
from shacklog.services import bands
from shacklog.services.links import links_for
from shacklog.web import filters as flt
from shacklog.web import forms
from shacklog.web import helpers as h
from shacklog.web.forms import Field, Section
from shacklog.web.views.stations import check_coordinates, fill_position

bp = Blueprint("contacts", __name__, url_prefix="/contacts")
h.register_entity_view("contact", "contacts.view")

FORM = [
    Section("Contact", [
        Field("callsign", "Callsign", type="upper", required=True, width="third",
              attrs={"autofocus": "autofocus"}),
        Field("name", "Name", width="third"),
        Field("email", "E-mail", type="email", width="third"),
        Field("qth", "QTH", width="half"),
        Field("locator", "Locator", type="locator", width="quarter", placeholder="JN45ol"),
        Field("country", "Country", width="quarter"),
    ]),
    Section("Position", [
        Field("latitude", "Latitude", type="lat", width="half"),
        Field("longitude", "Longitude", type="lon", width="half"),
    ], open=False, note="Filled from the locator when left empty."),
    Section("Notes", [
        Field("notes", "Notes", type="textarea", width="full",
              placeholder="Met at…, interested in…, schedules on…"),
        Field("tags", "Tags", type="tags", width="full", placeholder="club, friend, qsl-pending"),
    ]),
]

FILTERS = [
    flt.Filter("q", "Text", "text", (Contact.callsign, Contact.name, Contact.qth, Contact.locator,
                                     Contact.country, Contact.notes, Contact.email),
               placeholder="Call, name, QTH, notes…"),
    flt.Filter("tags", "Tags", "tags", (Contact.tags,), placeholder="tag1, tag2"),
]

SORTS = [
    flt.Sort("call", "Callsign", Contact.callsign),
    flt.Sort("name", "Name", func.lower(Contact.name)),
    flt.Sort("updated", "Changed", Contact.updated_at, default_desc=True),
]


def norm_call(call: str | None) -> str:
    """How callsigns are compared and shown in URLs: upper case, no spaces."""
    return (call or "").replace(" ", "").upper()


def call_like(column, text: str):
    """``IK2*``/``?`` wildcards, otherwise "contains" (same rules as the QSO call filter)."""
    text = norm_call(text)
    if "*" in text or "?" in text:
        return column.like(text.replace("*", "%").replace("?", "_"))
    return column.like(f"%{text}%")


def filtered_query():
    """Contact cards matching the current request's filters."""
    return flt.apply_filters(h.live(Contact), FILTERS, request.args, h.session())


# ---------------------------------------------------------------------------
# Index: contact cards + callsigns in the log
# ---------------------------------------------------------------------------

CARD_LIMIT = 500
CALL_SORTS = {"count": "QSOs", "last": "Last QSO", "call": "Callsign", "first": "First QSO"}


def callsign_rows(s, call_filter: str | None, sort: str, direction: str):
    """One row per callsign in the log: (call, count, first, last).

    The latest name/QTH and the band list are added per page by :func:`decorate_calls`.
    """
    count = func.count(Qso.id).label("n")
    first = func.min(Qso.time_on).label("first")
    last = func.max(Qso.time_on).label("last")
    query = (s.query(Qso.call, count, first, last).filter(Qso.deleted_at.is_(None))
             .group_by(Qso.call))
    if call_filter:
        query = query.filter(call_like(Qso.call, call_filter))
    column = {"count": count, "last": last, "first": first}.get(sort, Qso.call)
    ordered = column.desc() if direction == "desc" else column.asc()
    return query.order_by(ordered, Qso.call)


def decorate_calls(s, rows) -> list[dict]:
    """Add latest name/QTH, the band list and the contact card to aggregated rows."""
    calls = [r.call for r in rows]
    if not calls:
        return []
    # Latest *known* name and QTH: a later QSO with an empty name must not hide the name.
    names: dict[str, str] = {}
    qths: dict[str, str] = {}
    for call, name, qth in (s.query(Qso.call, Qso.name, Qso.qth)
                            .filter(Qso.deleted_at.is_(None), Qso.call.in_(calls))
                            .order_by(Qso.time_on.desc())):
        if name:
            names.setdefault(call, name)
        if qth:
            qths.setdefault(call, qth)
    band_rows = (s.query(Qso.call, Qso.band).filter(Qso.deleted_at.is_(None), Qso.call.in_(calls),
                                                    Qso.band.isnot(None))
                 .distinct().all())
    band_map: dict[str, list[str]] = {}
    for call, band in band_rows:
        band_map.setdefault(call, []).append(band)
    cards = {c.callsign: c for c in h.live(Contact).filter(Contact.callsign.in_(calls))}
    out = []
    for r in rows:
        out.append({
            "call": r.call, "count": r.n, "first": r.first, "last": r.last,
            "name": names.get(r.call), "qth": qths.get(r.call),
            "bands": sorted(band_map.get(r.call, []), key=_band_order),
            "card": cards.get(r.call),
        })
    return out


def _band_order(band: str) -> int:
    try:
        return bands.BAND_NAMES.index(band)
    except ValueError:
        return len(bands.BAND_NAMES)


@bp.route("/")
def index():
    s = h.session()
    result = filtered_query()
    cards_query, sort_key, sort_dir = flt.apply_sort(result, SORTS, request.args, "call")
    # Cards are few (one per person I keep notes on); the paginated part of this page is
    # the callsign table below, so the card list is capped instead of paginated.
    cards = cards_query.limit(CARD_LIMIT).all()
    matching_cards = cards_query.order_by(None).count() if len(cards) == CARD_LIMIT else len(cards)
    total_cards = h.live(Contact).count()

    call_filter = (request.args.get("call") or "").strip()
    csort = request.args.get("csort") if request.args.get("csort") in CALL_SORTS else "count"
    cdir = request.args.get("cdir") or ("asc" if csort == "call" else "desc")
    cdir = cdir if cdir in ("asc", "desc") else "desc"
    calls_page = h.paginate(callsign_rows(s, call_filter, csort, cdir))
    calls_page.items = decorate_calls(s, calls_page.items)
    return render_template(
        "contacts/index.html", cards=cards, total_cards=total_cards,
        matching_cards=matching_cards, filters=FILTERS,
        fchoices=flt.resolve_filter_choices(FILTERS, s), fresult=result,
        sort_key=sort_key, sort_dir=sort_dir, calls=calls_page, call_filter=call_filter,
        csort=csort, cdir=cdir, call_sorts=CALL_SORTS,
    )


# ---------------------------------------------------------------------------
# Contact card CRUD
# ---------------------------------------------------------------------------

def _render_form(card: Contact | None, values, errors, title: str):
    return render_template(
        "contacts/form.html", card=card, sections=FORM, values=values, errors=errors,
        choices=forms.resolve_choices(FORM, h.session()), title=title,
    )


def _parse(card: Contact | None):
    values, errors, raw = forms.parse(FORM, request.form)
    check_coordinates(values, errors)
    call = values.get("callsign")
    if call:
        existing = h.live(Contact).filter(Contact.callsign == call)
        if card is not None:
            existing = existing.filter(Contact.id != card.id)
        other = existing.first()
        if other is not None:
            link = url_for(".view", record_id=other.id)
            errors["callsign"] = Markup(
                'There is already a card for {call}: <a href="{link}">open it</a>.'
            ).format(call=escape(call), link=link)
    return values, errors, raw


@bp.route("/new", methods=["GET", "POST"])
def new():
    s = h.session()
    if request.method == "POST":
        values, errors, raw = _parse(None)
        if errors:
            return _render_form(None, raw, errors, "New contact card"), 422
        card = Contact()
        forms.apply(card, values)
        fill_position(card)
        s.add(card)
        s.commit()
        flash(f"Added a card for {card.callsign}.", "ok")
        return redirect(url_for(".callsign", call=card.callsign))

    call = norm_call(request.args.get("call"))
    template = Contact(callsign=call or None)
    if call:
        existing = h.live(Contact).filter(Contact.callsign == call).first()
        if existing is not None:
            return redirect(url_for(".edit", record_id=existing.id))
        # Prefill from the latest QSO, so the card starts with what the log knows.
        latest = h.live(Qso).filter(Qso.call == call).order_by(Qso.time_on.desc()).first()
        if latest is not None:
            template.name, template.qth = latest.name, latest.qth
            template.locator, template.country = latest.locator, latest.country
    return _render_form(None, forms.values_from_obj(FORM, template), {}, "New contact card")


@bp.route("/<record_id>/edit", methods=["GET", "POST"])
def edit(record_id: str):
    s = h.session()
    card = h.get_or_404(Contact, record_id)
    if request.method == "POST":
        values, errors, raw = _parse(card)
        if errors:
            return _render_form(card, raw, errors, f"Edit {card.callsign}"), 422
        before = (card.locator, card.latitude, card.longitude)
        forms.apply(card, values)
        fill_position(card, before)
        s.commit()
        flash("Saved.", "ok")
        return redirect(url_for(".callsign", call=card.callsign))
    return _render_form(card, forms.values_from_obj(FORM, card), {}, f"Edit {card.callsign}")


@bp.route("/<record_id>/delete", methods=["POST"])
def delete(record_id: str):
    card = h.get_or_404(Contact, record_id)
    card.soft_delete()
    h.session().commit()
    flash(f"Card for {card.callsign} moved to the trash. Its QSOs stay in the log.", "ok")
    return redirect(url_for(".index"))


@bp.route("/<record_id>")
def view(record_id: str):
    """A card's page is its callsign page (one place for everything about a call)."""
    card = h.get_or_404(Contact, record_id)
    return callsign_page(card.callsign, card)


# ---------------------------------------------------------------------------
# Callsign page
# ---------------------------------------------------------------------------

@bp.route("/call")
def jump():
    """Target of the "Everything about a callsign" box (works without JavaScript)."""
    call = norm_call(request.args.get("call"))
    return redirect(url_for(".callsign", call=call) if call else url_for(".index"))


@bp.route("/call/<path:call>")
def callsign(call: str):
    normalized = norm_call(call)
    if not normalized:
        return redirect(url_for(".index"))
    if normalized != call:  # one canonical URL per callsign
        return redirect(url_for(".callsign", call=normalized))
    card = h.live(Contact).filter(Contact.callsign == normalized).first()
    return callsign_page(normalized, card)


def callsign_page(call: str, card: Contact | None):
    s = h.session()
    qsos_q = h.live(Qso).filter(Qso.call == call)
    qsos = (qsos_q.options(joinedload(Qso.station), joinedload(Qso.repeater))
            .order_by(Qso.time_on.desc()).all())
    # callnorm() (db.py) = upper case without spaces, so "ik2 abc" heard still matches.
    heard = (h.live(SwlEntry).filter(func.callnorm(SwlEntry.heard) == call)
             .order_by(SwlEntry.time_utc.desc()).all())
    repeaters = (h.live(Repeater).filter(func.callnorm(Repeater.callsign) == call)
                 .order_by(Repeater.output_mhz).all())
    stats = None
    if qsos:
        distances = [q.distance_km for q in qsos if q.distance_km is not None]
        far = max((q for q in qsos if q.distance_km is not None),
                  key=lambda q: q.distance_km, default=None)
        stats = {
            "count": len(qsos), "first": qsos[-1].time_on, "last": qsos[0].time_on,
            "bands": sorted({q.band for q in qsos if q.band}, key=_band_order),
            "modes": sorted({bands.display_mode(q.mode, q.submode) for q in qsos
                             if q.mode or q.submode}),
            "max_km": max(distances) if distances else None, "far": far,
            "stations": sorted({q.station.name for q in qsos if q.station}),
            "repeaters": sorted({q.repeater.display_name() for q in qsos if q.repeater}),
        }
    latest = qsos[0] if qsos else None
    return render_template(
        "contacts/callsign.html", call=call, card=card, qsos=qsos, heard=heard,
        repeaters=repeaters, stats=stats, latest=latest, country=h.country_of(call),
        linked=links_for(s, "contact", card.id) if card else [],
    )
