"""Equipment inventory: radios, handhelds, SDRs, antennas, coax, meters, supplies.

Each item's page gathers everything that mentions it: manuals and photos (links panel),
CHIRP configurations of a radio, QSOs and listening entries made with it (as radio,
receiver or antenna), journal entries about it, and stations using it by default.
That is how "which antenna did I use for that contact?" and "where is the K1 manual?"
are answered in one click.
"""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from sqlalchemy import func, or_

from shacklog.models import (
    EQUIPMENT_KINDS,
    EQUIPMENT_STATUS,
    Equipment,
    JournalEntry,
    Qso,
    RadioConfig,
    Station,
    SwlEntry,
)
from shacklog.services.links import links_for
from shacklog.web import filters as flt
from shacklog.web import forms
from shacklog.web import helpers as h
from shacklog.web.forms import Field, Section

bp = Blueprint("equipment", __name__, url_prefix="/equipment")
h.register_entity_view("equipment", "equipment.view")

FORM = [
    Section("Item", [
        Field("name", "Name", required=True, width="half", placeholder="Kenwood TM-733E"),
        Field("kind", "Kind", type="select", choices=EQUIPMENT_KINDS, blank=False, width="quarter"),
        Field("status", "Status", type="select", choices=EQUIPMENT_STATUS, blank=False,
              width="quarter"),
        Field("manufacturer", "Manufacturer", width="third"),
        Field("model", "Model", width="third"),
        Field("serial", "Serial number", width="third"),
        Field("firmware", "Firmware", width="half", placeholder="F4HWN v4.2"),
        Field("freq_range", "Frequency range", width="half", placeholder="144-146, 430-440 MHz"),
        Field("connector", "Connector", width="half", placeholder="SMA-F, PL-259, N…"),
    ]),
    Section("Purchase", [
        Field("acquired_on", "Acquired on", type="date", width="quarter"),
        Field("price", "Price", width="quarter", placeholder="€48.99"),
        Field("vendor", "Vendor", width="half"),
        Field("url", "Web page", type="url", width="full"),
    ], open=False),
    Section("Details", [
        Field("specs", "Specifications", type="textarea", width="full",
              help="Power, sensitivity, gain, SWR, dimensions… one per line."),
        Field("notes", "Notes", type="textarea", width="full"),
        Field("tags", "Tags", type="tags", width="full", placeholder="portable, vhf, shack"),
    ]),
]

FILTERS = [
    flt.Filter("q", "Text", "text", (Equipment.name, Equipment.manufacturer, Equipment.model,
                                     Equipment.serial, Equipment.firmware, Equipment.freq_range,
                                     Equipment.specs, Equipment.notes, Equipment.vendor),
               placeholder="Name, model, serial…"),
    flt.Filter("kind", "Kind", "choice", (Equipment.kind,), choices=EQUIPMENT_KINDS),
    flt.Filter("status", "Status", "choice", (Equipment.status,), choices=EQUIPMENT_STATUS),
    flt.Filter("tags", "Tags", "tags", (Equipment.tags,), placeholder="tag1, tag2"),
]

SORTS = [
    flt.Sort("name", "Name", func.lower(Equipment.name)),
    flt.Sort("kind", "Kind", Equipment.kind),
    flt.Sort("status", "Status", Equipment.status),
    flt.Sort("acquired", "Acquired", Equipment.acquired_on, default_desc=True),
]


def filtered_query():
    """Equipment matching the current request's filters."""
    return flt.apply_filters(h.live(Equipment), FILTERS, request.args, h.session())


@bp.route("/")
def index():
    s = h.session()
    result = filtered_query()
    query, sort_key, sort_dir = flt.apply_sort(result, SORTS, request.args, "kind")
    if sort_key != "name":
        query = query.order_by(func.lower(Equipment.name))
    page = h.paginate(query)
    return render_template(
        "equipment/index.html", page=page, filters=FILTERS,
        fchoices=flt.resolve_filter_choices(FILTERS, s), fresult=result,
        sort_key=sort_key, sort_dir=sort_dir,
    )


def _render_form(item: Equipment | None, values, errors, title: str):
    return render_template(
        "equipment/form.html", item=item, sections=FORM, values=values, errors=errors,
        choices=forms.resolve_choices(FORM, h.session()), title=title,
    )


@bp.route("/new", methods=["GET", "POST"])
def new():
    s = h.session()
    if request.method == "POST":
        values, errors, raw = forms.parse(FORM, request.form)
        if errors:
            return _render_form(None, raw, errors, "New equipment"), 422
        item = Equipment()
        forms.apply(item, values)
        s.add(item)
        s.commit()
        flash(f"Added {item.name}.", "ok")
        return redirect(url_for(".view", record_id=item.id))
    kind = request.args.get("kind")
    template = Equipment(kind=kind if kind in dict(EQUIPMENT_KINDS) else "radio", status="active")
    return _render_form(None, forms.values_from_obj(FORM, template), {}, "New equipment")


@bp.route("/<record_id>/edit", methods=["GET", "POST"])
def edit(record_id: str):
    s = h.session()
    item = h.get_or_404(Equipment, record_id)
    if request.method == "POST":
        values, errors, raw = forms.parse(FORM, request.form)
        if errors:
            return _render_form(item, raw, errors, f"Edit {item.name}"), 422
        forms.apply(item, values)
        s.commit()
        flash("Saved.", "ok")
        return redirect(url_for(".view", record_id=item.id))
    return _render_form(item, forms.values_from_obj(FORM, item), {}, f"Edit {item.name}")


@bp.route("/<record_id>/delete", methods=["POST"])
def delete(record_id: str):
    item = h.get_or_404(Equipment, record_id)
    item.soft_delete()
    h.session().commit()
    flash(f"{item.name} moved to the trash.", "ok")
    return redirect(url_for(".index"))


@bp.route("/<record_id>")
def view(record_id: str):
    s = h.session()
    item = h.get_or_404(Equipment, record_id)
    qsos = h.live(Qso).filter(or_(Qso.radio_id == item.id, Qso.antenna_id == item.id))
    swl = h.live(SwlEntry).filter(or_(SwlEntry.receiver_id == item.id,
                                      SwlEntry.antenna_id == item.id))
    configs = (h.live(RadioConfig).filter(RadioConfig.radio_id == item.id)
               .order_by(RadioConfig.is_current.desc(), RadioConfig.config_date.desc().nulls_last(),
                         RadioConfig.created_at.desc()).all())
    journal = (h.live(JournalEntry).filter(JournalEntry.equipment_id == item.id)
               .order_by(JournalEntry.time_utc.desc()).limit(20).all())
    stations = (h.live(Station).filter(or_(Station.radio_id == item.id,
                                           Station.antenna_id == item.id))
                .order_by(Station.name).all())
    return render_template(
        "equipment/view.html", item=item, linked=links_for(s, "equipment", item.id),
        qso_count=qsos.count(),
        qso_as_radio=h.live(Qso).filter(Qso.radio_id == item.id).count(),
        qso_as_antenna=h.live(Qso).filter(Qso.antenna_id == item.id).count(),
        latest_qsos=qsos.order_by(Qso.time_on.desc()).limit(10).all(),
        swl_count=swl.count(),
        swl_as_receiver=h.live(SwlEntry).filter(SwlEntry.receiver_id == item.id).count(),
        swl_as_antenna=h.live(SwlEntry).filter(SwlEntry.antenna_id == item.id).count(),
        latest_swl=swl.order_by(SwlEntry.time_utc.desc()).limit(5).all(),
        configs=configs, journal=journal, stations=stations,
        is_radio=item.kind in h.RADIO_KINDS,
    )
